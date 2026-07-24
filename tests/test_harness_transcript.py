from __future__ import annotations

import json
from pathlib import Path

from pikvm_agent.harness.transcript import analyze_claude_transcript


def test_transcript_analyzer_turns_direct_tool_history_into_replay_findings(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "conversation.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "mcp__pikvm__pikvm_run_burst",
                        "input": {
                            "session_id": "s_1",
                            "based_on_world_version": 2,
                            "based_on_control_epoch": 1,
                            "actions": [
                                {
                                    "type": "type_text",
                                    "method": "print",
                                    "text": "rm -rf ./build",
                                },
                                {"type": "key", "keys": ["ENTER"]},
                            ],
                        },
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_2",
                        "name": "mcp__pikvm__pikvm_run_burst",
                        "input": {
                            "session_id": "s_1",
                            "actions": [
                                {
                                    "type": "type_text",
                                    "text": "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
                                    * 5,
                                }
                            ],
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "tool_3",
                        "name": "mcp__pikvm__pikvm_screenshot",
                        "input": {"session_id": "s_1"},
                    },
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows))

    report = analyze_claude_transcript(transcript)

    assert report.tool_counts["mcp__pikvm__pikvm_run_burst"] == 2
    assert report.tool_counts["mcp__pikvm__pikvm_screenshot"] == 1
    assert report.bursts == 2
    assert report.missing_idempotency_keys == 2
    assert report.missing_freshness == 1
    assert report.unverified_print_entries == 1
    assert report.base64_entries == 1
    assert report.submit_in_same_burst == 1
    assert report.dangerous_submissions == 1
    assert report.total_typed_characters == 14 + 140
    kinds = {finding.kind for finding in report.findings}
    assert {
        "missing_idempotency",
        "missing_freshness",
        "unverified_print",
        "base64_payload",
        "same_burst_submit",
        "dangerous_submission",
    } <= kinds
    # The report carries hashes/lengths for replay grouping, never typed bodies.
    assert "rm -rf" not in report.model_dump_json()
