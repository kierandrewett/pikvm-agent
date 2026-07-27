from __future__ import annotations

from typing import Any

import pytest

from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    ModelRequest,
    ModelResponse,
    RunSnapshot,
    RunStatus,
)
from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.assistant import AssistantHarness
from pikvm_agent.harness.general_tools import (
    ToolDescriptor,
    ToolResult,
)
from pikvm_agent.harness.model_budget import ModelBudgetPolicy
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute


class ScriptedAssistantProvider:
    name = "scripted-assistant"

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.decisions:
            raise AssertionError("assistant model was called more often than expected")
        return ModelResponse(
            provider=self.name,
            model="scripted-chat-v1",
            data=self.decisions.pop(0),
        )


class StubComputerHarness:
    def __init__(self, store: InMemoryRunStore) -> None:
        self.store = store
        self.budget_policy = ModelBudgetPolicy(max_provider_attempts=50)
        self.activated: list[tuple[str, str]] = []

    async def activate_computer(
        self,
        run_id: str,
        computer_task: str,
    ) -> RunSnapshot:
        self.activated.append((run_id, computer_task))
        run = await self.store.get_control(run_id)
        run.mode = "computer"
        run.computer_task = computer_task
        run.session_id = "computer-session"
        run.status = RunStatus.RUNNING
        run.record("computer.opened", session_id=run.session_id)
        await self.store.save(run)
        return run

    async def continue_run(self, run_id: str) -> RunSnapshot:
        run = await self.store.get_control(run_id)
        run.status = RunStatus.COMPLETED
        run.record("run.completed", mode="computer")
        await self.store.save(run)
        return run


class StubToolBroker:
    def __init__(
        self,
        descriptors: list[ToolDescriptor],
        results: dict[str, ToolResult] | None = None,
    ) -> None:
        self.descriptors = descriptors
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def catalog(self) -> list[ToolDescriptor]:
        return self.descriptors

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        self.calls.append((name, arguments))
        return self.results[name]


def assistant_harness(
    decisions: list[dict[str, Any]],
    *,
    tools: StubToolBroker | None = None,
) -> tuple[
    AssistantHarness,
    ScriptedAssistantProvider,
    StubComputerHarness,
    InMemoryRunStore,
]:
    store = InMemoryRunStore()
    provider = ScriptedAssistantProvider(decisions)
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            "reasoner": RoleRoute([provider.name]),
            "controller": RoleRoute([provider.name]),
            "verifier": RoleRoute([provider.name]),
        },
    )
    computer = StubComputerHarness(store)
    harness = AssistantHarness(
        models=pool,
        store=store,
        computer=computer,  # type: ignore[arg-type]
        tools=tools,
    )
    return harness, provider, computer, store


@pytest.mark.asyncio
async def test_normal_chat_reply_and_follow_up_do_not_open_the_computer() -> None:
    harness, provider, computer, _ = assistant_harness(
        [
            {
                "outcome": "reply",
                "message": "Quarterly earnings are reported every three months.",
            },
            {
                "outcome": "reply",
                "message": "I can also explain how fiscal quarters differ.",
            },
        ]
    )

    created = await harness.create("What are quarterly earnings?")
    first = await harness.continue_run(created.run_id)
    steered = await harness.steer(first.run_id, "What about fiscal quarters?")
    second = await harness.continue_run(steered.run_id)

    assert second.status is RunStatus.COMPLETED
    assert [(message.role, message.content) for message in second.conversation] == [
        ("user", "What are quarterly earnings?"),
        (
            "assistant",
            "Quarterly earnings are reported every three months.",
        ),
        ("user", "What about fiscal quarters?"),
        (
            "assistant",
            "I can also explain how fiscal quarters differ.",
        ),
    ]
    assert all(message.event_cursor >= 0 for message in second.conversation)
    assert provider.requests[-1].metadata == {"mode": "assistant"}
    assert "What about fiscal quarters?" in provider.requests[-1].prompt
    assert computer.activated == []


@pytest.mark.asyncio
async def test_read_only_tool_is_visible_and_old_result_is_not_replayed_next_turn() -> None:
    tools = StubToolBroker(
        [
            ToolDescriptor(
                name="search.lookup",
                title="Search",
                description="Look up a public source",
                read_only=True,
            )
        ],
        {
            "search.lookup": ToolResult(
                content='{"title":"Result","url":"https://example.test"}'
            )
        },
    )
    harness, provider, _, _ = assistant_harness(
        [
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "search.lookup",
                    "arguments_json": (
                        '{"query":"quarterly earnings"}'
                    ),
                },
            },
            {
                "outcome": "reply",
                "message": "The source reports quarterly results.",
            },
            {
                "outcome": "reply",
                "message": "A fresh answer without another lookup.",
            },
        ],
        tools=tools,
    )

    created = await harness.create("Find the latest report")
    first = await harness.continue_run(created.run_id)
    await harness.steer(first.run_id, "Now answer from general knowledge")
    second = await harness.continue_run(first.run_id)

    assert tools.calls == [
        ("search.lookup", {"query": "quarterly earnings"})
    ]
    event_kinds = [event.kind for event in second.events]
    assert "tool.started" in event_kinds
    assert "tool.completed" in event_kinds
    assert "https://example.test" not in provider.requests[-1].prompt
    provider_schema = provider.requests[0].output_schema
    wire_tool = provider_schema["$defs"]["_ProviderAssistantToolCall"]
    assert wire_tool["additionalProperties"] is False
    assert set(wire_tool["properties"]) == {"name", "arguments_json"}


@pytest.mark.asyncio
async def test_side_effect_tool_waits_for_exact_human_approval() -> None:
    tools = StubToolBroker(
        [
            ToolDescriptor(
                name="mail.send",
                title="Send email",
                description="Send an email",
                destructive=False,
                open_world=True,
            )
        ],
        {"mail.send": ToolResult(content='{"sent":true}')},
    )
    harness, _, _, _ = assistant_harness(
        [
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "mail.send",
                    "arguments_json": (
                        '{"to":"recipient@example.test","body":"Hello"}'
                    ),
                },
            },
            {
                "outcome": "reply",
                "message": "The message was sent after your approval.",
            },
        ],
        tools=tools,
    )

    created = await harness.create("Send the message")
    waiting = await harness.continue_run(created.run_id)
    pending = waiting.pending_approval or {}

    assert waiting.status is RunStatus.NEEDS_APPROVAL
    assert pending["tool"] == "mail.send"
    assert pending["arguments"] == {
        "to": "recipient@example.test",
        "body": "Hello",
    }
    assert tools.calls == []
    with pytest.raises(ValueError, match="approval_id"):
        await harness.resolve_approval(
            waiting.run_id,
            "wrong-approval",
            {"type": "approve"},
        )

    approved = await harness.resolve_approval(
        waiting.run_id,
        str(pending["approval_id"]),
        {"type": "approve"},
    )
    completed = await harness.continue_run(approved.run_id)

    assert tools.calls == [
        (
            "mail.send",
            {
                "to": "recipient@example.test",
                "body": "Hello",
            },
        )
    ]
    assert completed.status is RunStatus.COMPLETED
    assert any(event.kind == "tool.completed" for event in completed.events)


@pytest.mark.asyncio
async def test_tool_round_limit_stops_before_an_extra_external_call() -> None:
    tools = StubToolBroker(
        [
            ToolDescriptor(
                name="search.lookup",
                title="Search",
                read_only=True,
            )
        ],
        {"search.lookup": ToolResult(content='{"result":"ok"}')},
    )
    store = InMemoryRunStore()
    provider = ScriptedAssistantProvider(
        [
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "search.lookup",
                    "arguments_json": '{"query":"first"}',
                },
            },
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "search.lookup",
                    "arguments_json": '{"query":"must-not-run"}',
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
    assistant = AssistantHarness(
        models=pool,
        store=store,
        computer=StubComputerHarness(store),  # type: ignore[arg-type]
        tools=tools,
        max_tool_rounds=1,
    )

    created = await assistant.create("Search once")
    paused = await assistant.continue_run(created.run_id)

    assert paused.status is RunStatus.PAUSED
    assert paused.error == "assistant tool-round limit reached"
    assert tools.calls == [("search.lookup", {"query": "first"})]


@pytest.mark.asyncio
async def test_computer_handoff_preserves_the_exact_bounded_task() -> None:
    harness, _, computer, _ = assistant_harness(
        [
            {
                "outcome": "computer",
                "message": "I’ll use the computer for the spreadsheet.",
                "computer_task": (
                    "Create a quarterly earnings spreadsheet in the open workbook."
                ),
            }
        ]
    )

    created = await harness.create("Make the spreadsheet")
    completed = await harness.continue_run(created.run_id)

    assert completed.mode == "computer"
    assert completed.computer_task == (
        "Create a quarterly earnings spreadsheet in the open workbook."
    )
    assert computer.activated == [
        (
            created.run_id,
            "Create a quarterly earnings spreadsheet in the open workbook.",
        )
    ]
    assert completed.conversation[-1].content == (
        "I’ll use the computer for the spreadsheet."
    )


@pytest.mark.asyncio
async def test_completed_computer_work_returns_to_the_same_chat_thread() -> None:
    harness, provider, computer, _ = assistant_harness(
        [
            {
                "outcome": "computer",
                "message": "I’ll create that in the open workbook.",
                "computer_task": "Create the first spreadsheet.",
            },
            {
                "outcome": "reply",
                "message": "The first spreadsheet task is complete.",
            },
        ]
    )

    created = await harness.create("Create the spreadsheet")
    computer_result = await harness.continue_run(created.run_id)
    returned = await harness.steer(
        computer_result.run_id,
        "Thanks. What did you create?",
    )
    answered = await harness.continue_run(returned.run_id)

    assert returned.mode == "assistant"
    assert returned.status is RunStatus.PLANNING
    assert answered.conversation[-2].content == "Thanks. What did you create?"
    assert answered.conversation[-1].content == (
        "The first spreadsheet task is complete."
    )
    assert len(computer.activated) == 1
    assert "Thanks. What did you create?" in provider.requests[-1].prompt


@pytest.mark.asyncio
async def test_second_computer_handoff_refreshes_existing_session_and_resets_plan_state() -> None:
    class ReusableComputer:
        opened = 0
        refreshed: list[str] = []

        async def open(self, _: str) -> ComputerObservation:
            self.opened += 1
            raise AssertionError("an existing assistant session must be refreshed")

        async def refresh(self, *, session_id: str) -> ComputerObservation:
            self.refreshed.append(session_id)
            return ComputerObservation(
                session_id=session_id,
                status="paused",
                frame_id=9,
                world_version=12,
                control_epoch=4,
            )

    store = InMemoryRunStore()
    computer = ReusableComputer()
    run = RunSnapshot(
        run_id="assistant-reuse",
        task="Original conversation",
        mode="assistant",
        status=RunStatus.COMPLETED,
        session_id="existing-session",
        operator_guidance=["old computer correction"],
        conversation=[
            {
                "message_id": "user-1",
                "role": "user",
                "content": "Do the first task",
                "event_cursor": 1,
            }
        ],
    )
    await store.save(run)
    harness = AgentHarness(
        computer=computer,  # type: ignore[arg-type]
        models=object(),  # type: ignore[arg-type]
        store=store,
    )

    activated = await harness.activate_computer(
        run.run_id,
        "Do the second bounded task",
    )

    assert activated.mode == "computer"
    assert activated.computer_task == "Do the second bounded task"
    assert activated.status is RunStatus.RUNNING
    assert activated.observation is not None
    assert activated.observation.frame_id == 9
    assert activated.operator_guidance == []
    assert computer.refreshed == ["existing-session"]
    assert computer.opened == 0
