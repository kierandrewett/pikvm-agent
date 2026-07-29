from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness import showcase_runner
from pikvm_agent.harness.showcase_runner import (
    CampaignWriter,
    ShowcaseManifest,
    approval_is_safe,
    load_showcase_manifest,
)


def test_public_showcase_manifest_contains_fifty_distinct_codex_tasks() -> None:
    manifest = load_showcase_manifest(
        Path(__file__).parents[1] / "bench" / "codex-50-tasks.yaml"
    )

    assert manifest.provider == "codex-fast"
    assert len(manifest.tasks) == 50
    assert len({task.task_id for task in manifest.tasks}) == 50
    assert {
        "Observation",
        "Calculator",
        "Text entry",
        "Code entry",
        "File management",
        "Microsoft Excel",
        "Microsoft Word",
    } == {task.category for task in manifest.tasks}


def test_campaign_writer_declares_reboot_isolation_before_first_task(
    tmp_path: Path,
) -> None:
    manifest = ShowcaseManifest.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "campaign-1",
            "title": "One task",
            "provider": "codex-fast",
            "tasks": [
                {
                    "task_id": "task-1",
                    "title": "Observe",
                    "category": "Observation",
                    "prompt": "Describe the desktop.",
                }
            ],
        }
    )

    writer = CampaignWriter(manifest, tmp_path)
    payload = json.loads(writer.path.read_text(encoding="utf-8"))

    assert payload["isolation"]["reboot_after_every_task"] is True
    assert payload["tasks"][0]["reboot"]["status"] == "pending"
    assert not writer.path.with_suffix(".json.tmp").exists()


def test_campaign_writer_restores_existing_run_without_replacing_it(
    tmp_path: Path,
) -> None:
    manifest = ShowcaseManifest.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "campaign-1",
            "title": "One task",
            "provider": "codex-fast",
            "tasks": [
                {
                    "task_id": "task-1",
                    "title": "Observe",
                    "category": "Observation",
                    "prompt": "Describe the desktop.",
                }
            ],
        }
    )
    first = CampaignWriter(manifest, tmp_path)
    first.task("task-1")["status"] = "running"
    first.task("task-1")["run_id"] = "durable-run-7"
    first.payload["current_task_id"] = "task-1"
    first.payload["current_run_id"] = "durable-run-7"
    first.flush()

    restored = CampaignWriter(manifest, tmp_path)

    assert restored.task("task-1")["status"] == "running"
    assert restored.task("task-1")["run_id"] == "durable-run-7"
    assert restored.payload["current_run_id"] == "durable-run-7"


def test_workspace_approval_allowlist_rejects_communications_and_shutdown() -> None:
    local_edit = {
        "approval_id": "approval-1",
        "risk": "medium",
        "proposed_action": {
            "actions": [
                {"type": "type_text", "text": "proof"},
                {"type": "key", "keys": ["CTRL", "S"]},
            ]
        },
    }
    send_message = {
        **local_edit,
        "reason": "Send a Teams message",
    }
    shutdown = {
        **local_edit,
        "proposed_action": {
            "actions": [
                {
                    "type": "type_text",
                    "text": "shutdown /r /t 0",
                }
            ]
        },
    }

    assert approval_is_safe(local_edit, mutates_workspace=True)
    assert not approval_is_safe(local_edit, mutates_workspace=False)
    assert not approval_is_safe(send_message, mutates_workspace=True)
    assert not approval_is_safe(shutdown, mutates_workspace=True)


def test_showcase_cli_runs_async_campaign(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: cli-campaign
title: CLI campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return {"campaign_id": "cli-campaign", "status": "completed"}

    monkeypatch.setattr(showcase_runner, "run_showcase_campaign", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "showcase-run",
            "--manifest",
            str(manifest_path),
            "--output-root",
            str(tmp_path / "output"),
            "--harness-url",
            "http://127.0.0.1:48001",
            "--adapter-url",
            "http://127.0.0.1:48002",
            "--operator-origin",
            "http://127.0.0.1:48001",
        ],
        env={
            "PIKVM_HARNESS_AGENT_TOKEN": "a" * 32,
            "PIKVM_HARNESS_TOKEN": "b" * 32,
        },
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "completed"
    assert calls[0]["adapter_url"] == "http://127.0.0.1:48002"
