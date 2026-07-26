from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app

TEST_ACCESS_TOKEN = "test-harness-token-0123456789abcdef"


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
async def test_harness_serves_a_visible_authenticated_operator_console() -> None:
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

    assert root.status_code in {302, 307}
    assert root.headers["location"] == "/app/"
    assert page.status_code == 200
    assert script.status_code == 200
    assert styles.status_code == 200

    html = page.text
    javascript = script.text
    assert 'id="live-screen"' in html
    assert 'id="machine-target"' in html
    assert 'id="fact-machine"' in html
    assert 'id="fact-target"' in html
    assert 'id="fact-layer"' in html
    assert 'id="control-assurance"' in html
    assert 'id="control-assurance-title"' in html
    assert 'id="control-assurance-detail"' in html
    assert 'id="activity-status"' in html
    assert 'id="activity-label"' in html
    assert 'id="efficiency-strip"' in html
    assert 'id="metric-wall"' in html
    assert 'id="metric-model"' in html
    assert 'id="metric-progress"' in html
    assert 'id="metric-recovery"' in html
    assert 'id="metric-budget"' in html
    assert 'id="event-timeline"' in html
    assert 'id="approval-shelf"' in html
    assert 'id="pause-button"' in html
    assert 'id="steer-button"' in html
    assert 'aria-label="Emergency stop"' in html
    assert 'aria-label="Create new task"' in html
    assert "sessionStorage" in javascript
    assert "Authorization" in javascript
    assert "X-PiKVM-Approval-Intent" in javascript
    assert "X-PiKVM-Frame-Mode" in javascript
    assert "/pause" in javascript
    assert "/steer" in javascript
    assert "Guide this run" in javascript
    assert "ReadableStream" in javascript
    assert "visibilitychange" in javascript
    assert "health.ready" in javascript
    assert "health.cooldown_until" in javascript
    assert "health.routes" in javascript
    assert "health.interface" in javascript
    assert "health.pixel_input" in javascript
    assert "health.structured_output" in javascript
    assert "health.billing_mode" in javascript
    assert "health.support_tier" in javascript
    assert "health.credential_owner" in javascript
    assert "Tier ≠ live-tested" in javascript
    assert "health.configured_model" in javascript
    assert "health.conformance_status" in javascript
    assert "conformance_exact" in javascript
    assert "providerConformanceLabel" in javascript
    assert "Blind conformance" in javascript
    assert "Provider skipped" in javascript
    assert "machine.fingerprint" in javascript
    assert "machine.desktop_layer" in javascript
    assert "Schema repair" in javascript
    assert "Unsafe commit separated" in javascript
    assert "Pointer no-op rejected" in javascript
    assert "Prerequisites present · unproven" in javascript
    assert "OpenAI Responses API" in javascript
    assert "Azure OpenAI Responses API" in javascript
    assert "Gemini CLI" in javascript
    assert "Vertex AI Gemini API" in javascript
    assert "providerAuthLabel" in javascript
    assert "CLI bearer token" in javascript
    assert "Bearer token environment" in javascript
    assert "eligible now" in javascript
    assert "Harness-managed control" in javascript
    assert "Direct MCP control" in javascript
    assert "function runSource(run)" in javascript
    assert "Requested by ${source}" in javascript
    assert "Plan · ${source}" in javascript
    assert "Declared by MCP launcher" in javascript
    assert "No independent model verifier is running." in javascript
    assert "Read-only media transfer" in javascript
    assert "Cleanup required" in javascript
    assert "Exact guest-file receipts" in javascript
    assert "call in flight" in javascript
    assert "run.active_activity" in javascript
    assert "activity.arguments" in javascript
    assert "MCP tool" in javascript
    assert "renderActivityAge" in javascript
    assert "stream.ready" in javascript
    assert "stream.heartbeat" in javascript
    assert "reconnectDelay" in javascript
    assert "MAX_VISIBLE_EVENTS" in javascript
    assert "PERFORMANCE_REFRESH_MS" in javascript
    assert "/performance" in javascript
    assert "renderEfficiency" in javascript
    assert "renderBudgetMetric" in javascript
    assert "providerAuthLabel" in javascript
    assert "Model budget exhausted" in javascript
    assert "Model cost settlement failed" in javascript
    assert "Harness continued automatically" in javascript
    assert "Automatic continuation stopped" in javascript
    assert "performance.autonomous_resumes" in javascript
    assert "external_benchmark" in javascript
    assert "Live benchmark connected" in javascript
    assert "localStorage" not in javascript
    assert "EventSource" not in javascript
    assert "?token=" not in javascript


def test_harness_ui_uses_one_static_entrypoint_and_no_remote_assets() -> None:
    ui_dir = Path(__file__).parents[1] / "pikvm_agent" / "harness_ui"
    html = (ui_dir / "index.html").read_text()
    javascript = (ui_dir / "app.js").read_text()

    assert '<script src="./app.js?v=20260726h" defer></script>' in html
    assert '<link rel="stylesheet" href="./styles.css?v=20260726h">' in html
    assert 'id="efficiency-strip"' in html
    assert 'id="metric-wall"' in html
    assert 'id="metric-model"' in html
    assert 'id="metric-progress"' in html
    assert 'id="metric-recovery"' in html
    assert 'id="metric-budget"' in html
    assert 'minlength="32"' in html
    assert "token.length < 16" not in javascript
    assert "stream.ready" in javascript
    assert "stream.heartbeat" in javascript
    assert "reconnectDelay" in javascript
    assert "MAX_VISIBLE_EVENTS" in javascript
    assert "PERFORMANCE_REFRESH_MS" in javascript
    assert "/performance" in javascript
    assert "renderEfficiency" in javascript
    assert "Harness continued automatically" in javascript
    assert "Automatic continuation stopped" in javascript
    assert "performance.autonomous_resumes" in javascript
    assert "/verification-image" in javascript
    assert "verificationObjectUrl" in javascript
    assert "Before → after evidence" in javascript
    assert "Labelled verifier view" in javascript
    assert "Saved artifact acceptance" in javascript
    assert "Host-verified file" in javascript
    assert "artifact_acceptance" in javascript
    assert "artifact.passed" in javascript
    assert "Tool outcome" in javascript
    assert "Completed in" in javascript
    assert "action.failed" in javascript
    assert 'id="steer-button"' in html
    assert "/steer" in javascript
    assert "Guide this run" in javascript
    assert "operator_guidance" in javascript
    assert "run.steered" in javascript
    assert "https://" not in html
    assert "http://" not in html
    assert "Content-Security-Policy" in html


def test_harness_ui_has_a_release_bundle_budget_and_releases_frame_blobs() -> None:
    ui_dir = Path(__file__).parents[1] / "pikvm_agent" / "harness_ui"
    assets = [
        ui_dir / "index.html",
        ui_dir / "app.js",
        ui_dir / "styles.css",
    ]
    sizes = {path.name: path.stat().st_size for path in assets}
    javascript = (ui_dir / "app.js").read_text()

    assert sum(sizes.values()) <= 128 * 1024
    assert sizes["app.js"] <= 80 * 1024
    assert sizes["styles.css"] <= 36 * 1024
    assert "URL.revokeObjectURL(state.frameObjectUrl)" in javascript
    assert "URL.revokeObjectURL(state.verificationObjectUrl)" in javascript
    assert "MAX_VISIBLE_EVENTS = 500" in javascript


def test_harness_ui_references_only_declared_css_custom_properties() -> None:
    styles = (
        Path(__file__).parents[1]
        / "pikvm_agent"
        / "harness_ui"
        / "styles.css"
    ).read_text()
    declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", styles))
    referenced = set(re.findall(r"var\((--[a-z0-9-]+)", styles))

    assert referenced <= declared


def test_harness_ui_keeps_visibility_and_approvals_readable_at_reflow_widths() -> None:
    ui_dir = Path(__file__).parents[1] / "pikvm_agent" / "harness_ui"
    html = (ui_dir / "index.html").read_text()
    styles = (ui_dir / "styles.css").read_text()

    assert "<span>New task</span>" in html
    assert "<span>Stop</span>" in html
    assert ".command-actions .button span" in styles
    narrow_rules = styles.split("@media (max-width: 480px)", 1)[1]
    assert "#provider-button" not in narrow_rules.split(
        "@media (prefers-reduced-motion", 1
    )[0]
    assert "flex-wrap: wrap" in styles
    assert ".approval-copy p" in styles
    assert "white-space: normal" in styles
    assert "max-height: calc(100dvh - 24px)" in styles
    assert "overscroll-behavior: contain" in styles
