from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app

TEST_ACCESS_TOKEN = "test-harness-token-0123456789abcdef"
PROJECT_ROOT = Path(__file__).parents[1]
UI_DIR = PROJECT_ROOT / "pikvm_agent" / "harness_ui"
FRONTEND_DIR = PROJECT_ROOT / "frontend"


class NoopHarness:
    async def create(self, task: str) -> RunSnapshot:
        return RunSnapshot(run_id="unused", task=task, status=RunStatus.PAUSED)

    async def continue_run(self, run_id: str) -> RunSnapshot:
        raise AssertionError("not used")

    async def pause(self, run_id: str, reason: str) -> RunSnapshot:
        raise AssertionError("not used")

    async def resolve_approval(
        self, run_id: str, approval_id: str, decision: dict[str, Any]
    ) -> RunSnapshot:
        raise AssertionError("not used")

    async def abort(self, run_id: str, reason: str) -> RunSnapshot:
        raise AssertionError("not used")


class NoopModels:
    def health(self) -> dict[str, dict[str, object]]:
        return {}


@pytest.mark.asyncio
async def test_harness_serves_the_compiled_authenticated_chat_workspace() -> None:
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=InMemoryRunStore(),
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        follow_redirects=False,
    ) as client:
        root = await client.get("/")
        page = await client.get("/app/")
        script = await client.get("/app/app.js")
        styles = await client.get("/app/styles.css")
        font = await client.get(
            "/app/assets/geist-latin-wght-normal.woff2"
        )

    assert root.status_code in {302, 307}
    assert root.headers["location"] == "/app/"
    assert page.status_code == 200
    assert script.status_code == 200
    assert styles.status_code == 200
    assert font.status_code == 200
    assert font.headers["content-type"].startswith("font/woff2")

    assert '<div id="root"></div>' in page.text
    assert 'src="/app/app.js"' in page.text
    assert 'href="/app/styles.css"' in page.text
    assert "Content-Security-Policy" in page.text
    assert "sessionStorage" in script.text
    assert "Authorization" in script.text
    assert "X-PiKVM-Approval-Intent" in script.text
    assert "model_provider" in script.text
    assert "/steer" in script.text
    assert "localStorage" not in script.text
    assert "?token=" not in script.text


def test_workspace_uses_production_chat_and_component_libraries() -> None:
    package = json.loads((FRONTEND_DIR / "package.json").read_text())
    shell = (
        FRONTEND_DIR
        / "src"
        / "components"
        / "workspace"
        / "workspace-shell.tsx"
    ).read_text()
    thread = (
        FRONTEND_DIR
        / "src"
        / "components"
        / "assistant-ui"
        / "thread.tsx"
    ).read_text()

    assert package["dependencies"]["@assistant-ui/react"].startswith("^0.14")
    assert package["dependencies"]["@base-ui/react"]
    assert "useExternalStoreRuntime" in shell
    assert "ToolFallback: ComputerToolCall" in shell
    assert "ToolGroup: ComputerToolGroup" in shell
    assert "<ThreadList />" in shell
    assert "onRespondToToolApproval" in shell
    assert "AssistantRuntimeProvider" in shell
    assert "ThreadPrimitive.Messages" in thread
    assert "ComposerPrimitive.Input" in thread
    assert "ToolFallback" in thread
    assert "message-bubble" not in shell
    assert "innerHTML" not in shell

    computer_tool = (
        FRONTEND_DIR
        / "src"
        / "components"
        / "workspace"
        / "computer-tool-call.tsx"
    ).read_text()
    assert "Exact MCP arguments" in computer_tool
    assert "Computer action receipt" in computer_tool
    assert "Source screen" in computer_tool
    assert "Bounded input" in computer_tool
    assert "Exact input sequence" in computer_tool
    assert "Typed payload" in computer_tool
    assert "Held before a consequential input" in computer_tool
    assert "Screen check" in computer_tool
    assert "based_on_world_version" in computer_tool
    assert "ToolFallbackApproval" in computer_tool


def test_default_surface_is_chat_with_contextual_computer_and_diagnostics() -> None:
    shell = (
        FRONTEND_DIR
        / "src"
        / "components"
        / "workspace"
        / "workspace-shell.tsx"
    ).read_text()

    assert 'aria-label="Agent conversation"' in shell
    assert 'tooltip="Computer"' in shell
    assert 'tooltip="Diagnostics"' in shell
    assert "<ComputerSheet" in shell
    assert "<DiagnosticsSheet" in shell
    assert "open={computerOpen}" in shell
    assert "open={diagnosticsOpen}" in shell
    assert "workspace-rail" in shell
    assert "md:hidden" in shell


def test_compiled_workspace_has_no_remote_assets_and_a_bounded_bundle() -> None:
    html = (UI_DIR / "index.html").read_text()
    javascript = (UI_DIR / "app.js").read_bytes()
    styles = (UI_DIR / "styles.css").read_bytes()
    assets = [path for path in UI_DIR.rglob("*") if path.is_file()]

    assert "https://" not in html
    assert "http://" not in html
    assert html.count("<script") == 1
    assert all(path.stat().st_size <= 1_100_000 for path in assets)
    assert sum(path.stat().st_size for path in assets) <= 1_250_000
    assert len(gzip.compress(javascript)) <= 320 * 1024
    assert len(gzip.compress(styles)) <= 24 * 1024


def test_computer_frame_blob_lifecycle_is_explicit() -> None:
    computer_sheet = (
        FRONTEND_DIR
        / "src"
        / "components"
        / "workspace"
        / "computer-sheet.tsx"
    ).read_text()

    assert "URL.createObjectURL(blob)" in computer_sheet
    assert computer_sheet.count("URL.revokeObjectURL") >= 3
    assert "controller.abort()" in computer_sheet
    assert "window.clearInterval(timer)" in computer_sheet


def test_tool_events_are_mapped_to_structured_parts_not_raw_console_rows() -> None:
    mapper = (
        FRONTEND_DIR / "src" / "lib" / "run-messages.ts"
    ).read_text()

    assert 'type: "tool-call"' in mapper
    assert "toolCallId" in mapper
    assert "toolName" in mapper
    assert "argsText" in mapper
    assert "result" in mapper
    assert "approval" in mapper
    assert "action.attempted" in mapper


def test_chat_workspace_uses_the_authenticated_event_stream_for_live_work() -> None:
    api = (FRONTEND_DIR / "src" / "lib" / "harness-api.ts").read_text()
    workspace = (
        FRONTEND_DIR / "src" / "hooks" / "use-harness-workspace.ts"
    ).read_text()
    shell = (
        FRONTEND_DIR
        / "src"
        / "components"
        / "workspace"
        / "workspace-shell.tsx"
    ).read_text()

    assert 'headers.set("Accept", "text/event-stream")' in api
    assert "harnessEventStream" in workspace
    assert "/stream?after=" in workspace
    assert "liveUpdateStatus" in workspace
    assert "750" not in workspace
    assert "LiveUpdateBadge" in shell
    assert "Managed MCP connected" not in shell


def test_hidden_diagnostics_do_not_build_an_unbounded_event_console() -> None:
    diagnostics = (
        FRONTEND_DIR
        / "src"
        / "components"
        / "workspace"
        / "diagnostics-sheet.tsx"
    ).read_text()

    assert "open && run ? run.events.slice(-250).reverse() : []" in diagnostics
    assert "visibleEvents.map" in diagnostics
