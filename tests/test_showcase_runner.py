from __future__ import annotations

import json
from pathlib import Path

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
