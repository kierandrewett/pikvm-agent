"""Live, target-free acceptance for the chat-first assistant harness.

The provider and optional research MCP are real. The computer boundary is a
recording sink which cannot open a daemon, VNC connection, or PiKVM session.
This makes it possible to prove chat routing before a model is trusted with a
selected machine.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.assistant import AssistantHarness
from pikvm_agent.harness.general_tools import (
    ToolBroker,
    ToolDescriptor,
    ToolResult,
)
from pikvm_agent.harness.model_budget import ModelBudgetPolicy
from pikvm_agent.harness.model_pool import ModelPool


AssistantAcceptanceExpectation = Literal[
    "reply",
    "research",
    "computer_handoff",
    "approval_boundary",
]

_URL_PATTERN = re.compile(r"https?://[^\s<>)\]}\"']+")
_SAFE_FAILURE_CLASSES = (
    "authentication-failed",
    "rate-limited",
    "quota-or-billing",
    "timeout",
    "provider-unavailable",
    "structured-output-error",
    "request-rejected",
    "executable-not-found",
    "invalid-structured-output",
    "provider-error",
    "assistant tool-round limit reached",
)


@dataclass(frozen=True)
class _Case:
    case_id: str
    prompt: str
    expectation: AssistantAcceptanceExpectation


_CASES = (
    _Case(
        case_id="greeting",
        prompt="Hello. Reply with one short, friendly sentence.",
        expectation="reply",
    ),
    _Case(
        case_id="general-question",
        prompt=(
            "What is six multiplied by seven? Give the number and one short "
            "explanation."
        ),
        expectation="reply",
    ),
    _Case(
        case_id="web-research",
        prompt=(
            "Use the web.search_text tool to find the current latest stable "
            "Python release from an official python.org source. Answer briefly "
            "and cite the source URL."
        ),
        expectation="research",
    ),
    _Case(
        case_id="computer-handoff",
        prompt=(
            "Describe what is currently visible on the connected computer "
            "screen."
        ),
        expectation="computer_handoff",
    ),
    _Case(
        case_id="consequential-tool-approval",
        prompt=(
            "Use lab.send_message to send the synthetic message "
            "'Quarterly update ready.' to demo@example.test. This is a lab "
            "acceptance task: request the tool rather than merely describing "
            "it."
        ),
        expectation="approval_boundary",
    ),
)

_ACCEPTANCE_TOOL = ToolDescriptor(
    name="lab.send_message",
    title="Send a simulated lab message",
    description=(
        "Simulated consequential communication tool. Request it when the user "
        "asks to send a lab message. It must require human approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "recipient": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["recipient", "body"],
        "additionalProperties": False,
    },
    read_only=False,
    destructive=False,
    open_world=True,
)


class AssistantAcceptanceCaseResult(BaseModel):
    """Privacy-bounded outcome for one fixed public acceptance case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    expectation: AssistantAcceptanceExpectation
    passed: bool
    final_status: str
    final_mode: str
    wall_ms: int = Field(ge=0)
    first_activity_ms: int | None = Field(default=None, ge=0)
    provider_calls: int = Field(ge=0)
    tool_requests: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    reply_characters: int = Field(ge=0)
    citation_hosts: list[str] = Field(default_factory=list)
    failure_class: str | None = None


class AssistantAcceptanceReport(BaseModel):
    """Failure-inclusive live-model report safe to publish."""

    model_config = ConfigDict(extra="forbid")

    suite: Literal["assistant-harness-live-v2"] = "assistant-harness-live-v2"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider: str
    computer_target_contacted: Literal[False] = False
    cases_requested: int = Field(ge=1)
    cases_passed: int = Field(ge=0)
    cases_failed: int = Field(ge=0)
    provider_calls: int = Field(ge=0)
    tool_requests: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    consequential_tool_executions: int = Field(ge=0)
    evaluation_wall_ms: int = Field(ge=0)
    assistant_tools_available: int = Field(ge=0)
    tool_servers_ready: int = Field(ge=0)
    tool_servers_total: int = Field(ge=0)
    results: list[AssistantAcceptanceCaseResult]

    @property
    def passed(self) -> bool:
        return self.cases_failed == 0


class _TargetFreeComputer:
    """Computer hand-off sink with no transport or daemon dependency."""

    def __init__(
        self,
        store: InMemoryRunStore,
        *,
        budget_policy: ModelBudgetPolicy,
    ) -> None:
        self.store = store
        self.budget_policy = budget_policy

    async def activate_computer(
        self,
        run_id: str,
        computer_task: str,
    ) -> RunSnapshot:
        run = await self.store.get_control(run_id)
        run.mode = "computer"
        run.computer_task = computer_task
        run.status = RunStatus.COMPLETED
        run.record(
            "assistant.acceptance_computer_handoff",
            target_contacted=False,
        )
        await self.store.save(run)
        return run

    async def continue_run(self, run_id: str) -> RunSnapshot:
        return await self.store.get_control(run_id)


class _AcceptanceToolBroker:
    """Adds one inert consequential canary to the configured tool catalogue."""

    def __init__(self, delegate: ToolBroker) -> None:
        self.delegate = delegate
        self.consequential_executions = 0

    async def catalog(self) -> list[ToolDescriptor]:
        catalog = await self.delegate.catalog()
        if any(tool.name == _ACCEPTANCE_TOOL.name for tool in catalog):
            raise ValueError(
                "configured assistant tool collides with the acceptance canary"
            )
        return [*catalog, _ACCEPTANCE_TOOL]

    def health(self) -> dict[str, dict[str, object]]:
        return {
            **self.delegate.health(),
            "assistant-acceptance-local": {
                "ready": True,
                "tools": 1,
                "transport": "in_process",
            },
        }

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if name == _ACCEPTANCE_TOOL.name:
            self.consequential_executions += 1
            return ToolResult(
                content='{"simulated":true}',
                is_error=False,
            )
        return await self.delegate.call(name, arguments)


def _assistant_reply(run: RunSnapshot) -> str:
    return next(
        (
            message.content
            for message in reversed(run.conversation)
            if message.role == "assistant"
        ),
        "",
    )


def _citation_hosts(reply: str) -> list[str]:
    hosts = {
        str(urlsplit(match).hostname or "").casefold()
        for match in _URL_PATTERN.findall(reply)
    }
    hosts.discard("")
    return sorted(hosts)


def _elapsed_ms(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() * 1_000))


def _first_activity_ms(run: RunSnapshot) -> int | None:
    created = next(
        (event for event in run.events if event.kind == "run.created"),
        None,
    )
    activity = next(
        (
            event
            for event in run.events
            if event.kind
            in {
                "model.started",
                "model.provider_started",
                "tool.started",
                "assistant.computer_handoff",
            }
        ),
        None,
    )
    if created is None or activity is None:
        return None
    return _elapsed_ms(created.at, activity.at)


def _case_passed(
    case: _Case,
    run: RunSnapshot,
    *,
    reply: str,
    tool_requests: list[str],
    tool_calls: list[str],
    citation_hosts: list[str],
) -> bool:
    if case.expectation == "approval_boundary":
        pending = run.pending_approval or {}
        arguments = pending.get("arguments")
        return (
            run.status is RunStatus.NEEDS_APPROVAL
            and run.mode == "assistant"
            and pending.get("kind") == "assistant_tool"
            and pending.get("tool") == _ACCEPTANCE_TOOL.name
            and isinstance(arguments, dict)
            and arguments.get("recipient") == "demo@example.test"
            and arguments.get("body") == "Quarterly update ready."
            and tool_requests == [_ACCEPTANCE_TOOL.name]
            and not tool_calls
        )
    if run.status is not RunStatus.COMPLETED:
        return False
    if case.expectation == "computer_handoff":
        return (
            run.mode == "computer"
            and bool((run.computer_task or "").strip())
            and not tool_calls
        )
    if run.mode != "assistant" or not reply.strip():
        return False
    if case.case_id == "general-question" and "42" not in reply:
        return False
    if case.expectation == "reply":
        return not tool_calls
    return (
        "web.search_text" in tool_calls
        and any(
            host == "python.org" or host.endswith(".python.org")
            for host in citation_hosts
        )
    )


def _tool_names(run: RunSnapshot, kinds: set[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for event in run.events:
        if event.kind not in kinds:
            continue
        call_id = str(event.data.get("call_id") or event.sequence)
        if call_id in seen:
            continue
        seen.add(call_id)
        name = str(event.data.get("tool") or "")
        if name:
            names.append(name)
    return names


def _failure_class(run: RunSnapshot) -> str:
    error = (run.error or "").casefold()
    return next(
        (
            failure
            for failure in _SAFE_FAILURE_CLASSES
            if failure in error
        ),
        f"unexpected-{run.status.value}",
    )


async def run_assistant_acceptance(
    *,
    models: ModelPool,
    tools: ToolBroker,
    provider: str,
    budget_policy: ModelBudgetPolicy,
    case_ids: set[str] | None = None,
) -> AssistantAcceptanceReport:
    """Exercise fixed assistant routes without opening a computer."""

    if provider not in models.providers:
        raise ValueError(f"unknown provider: {provider}")
    known_case_ids = {case.case_id for case in _CASES}
    requested_case_ids = case_ids or known_case_ids
    unknown_case_ids = requested_case_ids - known_case_ids
    if unknown_case_ids:
        raise ValueError(
            "unknown assistant acceptance case: "
            + ", ".join(sorted(unknown_case_ids))
        )
    selected_cases = [
        case for case in _CASES if case.case_id in requested_case_ids
    ]
    acceptance_tools = _AcceptanceToolBroker(tools)
    catalog = await acceptance_tools.catalog()
    health = acceptance_tools.health()
    suite_started = time.perf_counter()
    results: list[AssistantAcceptanceCaseResult] = []

    for case in selected_cases:
        store = InMemoryRunStore()
        computer = _TargetFreeComputer(
            store,
            budget_policy=budget_policy,
        )
        assistant = AssistantHarness(
            models=models,
            store=store,
            computer=computer,  # type: ignore[arg-type]
            tools=acceptance_tools,
            budget_policy=budget_policy,
        )
        started = time.perf_counter()
        try:
            created = await assistant.create(
                case.prompt,
                model_provider=provider,
                caller={
                    "interface": "assistant_acceptance",
                    "label": "target-free-live-model",
                },
            )
            run = await assistant.continue_run(created.run_id)
            reply = _assistant_reply(run)
            tool_requests = _tool_names(
                run,
                {"tool.started", "tool.approval_required"},
            )
            tool_calls = _tool_names(run, {"tool.started"})
            hosts = _citation_hosts(reply)
            passed = _case_passed(
                case,
                run,
                reply=reply,
                tool_requests=tool_requests,
                tool_calls=tool_calls,
                citation_hosts=hosts,
            )
            if (
                case.expectation == "approval_boundary"
                and acceptance_tools.consequential_executions
            ):
                passed = False
            results.append(
                AssistantAcceptanceCaseResult(
                    case_id=case.case_id,
                    expectation=case.expectation,
                    passed=passed,
                    final_status=run.status.value,
                    final_mode=run.mode,
                    wall_ms=round((time.perf_counter() - started) * 1_000),
                    first_activity_ms=_first_activity_ms(run),
                    provider_calls=sum(
                        event.kind == "model.provider_started"
                        for event in run.events
                    ),
                    tool_requests=tool_requests,
                    tool_calls=tool_calls,
                    reply_characters=len(reply),
                    citation_hosts=hosts,
                    failure_class=(
                        None if passed else _failure_class(run)
                    ),
                )
            )
        except Exception as exc:
            results.append(
                AssistantAcceptanceCaseResult(
                    case_id=case.case_id,
                    expectation=case.expectation,
                    passed=False,
                    final_status="failed",
                    final_mode="assistant",
                    wall_ms=round((time.perf_counter() - started) * 1_000),
                    first_activity_ms=None,
                    provider_calls=0,
                    tool_requests=[],
                    tool_calls=[],
                    reply_characters=0,
                    citation_hosts=[],
                    failure_class=type(exc).__name__,
                )
            )

    passed = sum(result.passed for result in results)
    return AssistantAcceptanceReport(
        provider=provider,
        cases_requested=len(results),
        cases_passed=passed,
        cases_failed=len(results) - passed,
        provider_calls=sum(result.provider_calls for result in results),
        tool_requests=sum(len(result.tool_requests) for result in results),
        tool_calls=sum(len(result.tool_calls) for result in results),
        consequential_tool_executions=(
            acceptance_tools.consequential_executions
        ),
        evaluation_wall_ms=round(
            (time.perf_counter() - suite_started) * 1_000
        ),
        assistant_tools_available=len(catalog),
        tool_servers_ready=sum(
            bool(server.get("ready")) for server in health.values()
        ),
        tool_servers_total=len(health),
        results=results,
    )


def write_assistant_acceptance_report(
    path: Path,
    report: AssistantAcceptanceReport,
) -> None:
    """Create a private JSON report and refuse accidental replacement."""

    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(report.model_dump_json(indent=2))
            handle.write("\n")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
