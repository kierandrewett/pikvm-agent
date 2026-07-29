from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app
from tests.test_harness_api import (
    TEST_ACCESS_TOKEN,
    TEST_AGENT_TOKEN,
    StubHarness,
    StubModels,
)


def campaign_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": "codex-50",
        "title": "50 real Windows tasks",
        "status": "running",
        "total": 1,
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "current_task_id": "screen-01",
        "current_run_id": "run-1",
        "started_at": "2026-07-29T12:00:00Z",
        "updated_at": "2026-07-29T12:00:01Z",
        "tasks": [
            {
                "task_id": "screen-01",
                "title": "Read the desktop",
                "category": "Observation",
                "prompt": "Describe the desktop.",
                "status": "running",
                "run_id": "run-1",
                "recording": "screen-01/recording.mp4",
                "poster": "screen-01/poster.jpg",
            }
        ],
    }


@pytest.mark.asyncio
async def test_showcase_campaign_and_media_are_authenticated(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    showcase = tmp_path / "showcases" / "codex-50"
    task_dir = showcase / "screen-01"
    task_dir.mkdir(parents=True)
    (showcase / "campaign.json").write_text(
        json.dumps(campaign_payload()),
        encoding="utf-8",
    )
    (task_dir / "recording.mp4").write_bytes(b"video")
    (task_dir / "poster.jpg").write_bytes(b"poster")
    store = InMemoryRunStore()
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        agent_token=TEST_AGENT_TOKEN,
        allowed_origins={"http://harness"},
        showcase_dir=tmp_path / "showcases",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        unauthenticated = await client.get("/api/showcases/current")
        campaign = await client.get(
            "/api/showcases/current",
            headers={"authorization": f"Bearer {TEST_AGENT_TOKEN}"},
        )
        recording = await client.get(
            "/api/showcases/codex-50/tasks/screen-01/recording",
            headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
        )

    assert unauthenticated.status_code == 401
    assert campaign.status_code == 200
    assert campaign.json()["current_run_id"] == "run-1"
    assert recording.status_code == 200
    assert recording.content == b"video"
    assert recording.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_showcase_media_cannot_escape_campaign_directory(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    showcase = tmp_path / "showcases" / "codex-50"
    showcase.mkdir(parents=True)
    payload = campaign_payload()
    task = payload["tasks"][0]  # type: ignore[index]
    task["recording"] = "../../secret.mp4"  # type: ignore[index]
    (showcase / "campaign.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (tmp_path / "secret.mp4").write_bytes(b"secret")
    store = InMemoryRunStore()
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
        showcase_dir=tmp_path / "showcases",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        response = await client.get(
            "/api/showcases/codex-50/tasks/screen-01/recording",
        )

    assert response.status_code == 404
