from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from pikvm_agent.executor.burst import BurstError, validate_actions

CORPUS_PATH = (
    Path(__file__).resolve().parents[1] / "bench" / "historical_pikvm_incidents.json"
)


def load_corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def test_inventory_counts_are_internally_consistent() -> None:
    corpus = load_corpus()
    sessions = corpus["sessions"]
    scope = corpus["scope"]

    assert len(sessions) == scope["sessions"] == 24
    assert sum(row["pikvm_tool_calls"] for row in sessions) == 4_453
    assert sum(row["pikvm_tool_calls"] for row in sessions) == scope["pikvm_tool_calls"]

    by_client = Counter(row["client"] for row in sessions)
    calls_by_client = Counter()
    for row in sessions:
        calls_by_client[row["client"]] += row["pikvm_tool_calls"]

    for client, summary in scope["clients"].items():
        assert by_client[client] == summary["sessions"]
        assert calls_by_client[client] == summary["pikvm_tool_calls"]

    model_calls = Counter()
    for row in sessions:
        model_calls.update(row["model_call_counts"])
    assert dict(model_calls) == scope["model_call_counts"]


def test_incident_schema_sources_and_ids() -> None:
    corpus = load_corpus()
    incidents = corpus["incidents"]
    session_keys = {
        (row["client"], row["session_id"]) for row in corpus["sessions"]
    }
    allowed_categories = {
        "typing_truncation",
        "duplicate_or_extra_input",
        "focus_loss",
        "wrong_app_or_window",
        "wrong_target",
        "ocr_misread",
        "grounding_click",
        "selection_or_editor",
        "keyboard_layout_or_case",
        "operator_loop",
        "transport_or_schema",
        "pager_or_modal",
        "unsafe_or_unapproved",
        "command_or_script_corruption",
        "concurrency",
    }
    allowed_severities = {"low", "medium", "high", "critical"}
    allowed_causes = {"model", "tool_or_transport", "mixed", "unknown"}
    allowed_outcomes = {
        "recovered",
        "contained",
        "unresolved",
        "abandoned",
        "no_state_change",
    }
    allowed_risks = {
        "none",
        "external_message",
        "destructive_command",
        "commit_or_push",
        "cloud_mutation",
        "shared_task_mutation",
        "file_overwrite",
    }

    ids = [incident["id"] for incident in incidents]
    assert len(ids) == len(set(ids))
    assert len(incidents) >= 50

    for incident in incidents:
        source = incident["source"]
        assert (source["client"], source["session_id"]) in session_keys
        assert incident["category"] in allowed_categories
        assert incident["severity"] in allowed_severities
        assert incident["cause_attribution"] in allowed_causes
        assert incident["outcome"] in allowed_outcomes
        assert incident["one_shot_risk"] in allowed_risks
        assert source["record_indices"] == sorted(set(source["record_indices"]))
        assert source["tool_sequences"] == sorted(set(source["tool_sequences"]))
        assert all(index > 0 for index in source["record_indices"])
        assert all(sequence > 0 for sequence in source["tool_sequences"])
        assert incident["failure"]
        assert incident["correction"]
        assert incident["regression_requirements"]


def test_incident_summary_is_derived_from_records() -> None:
    corpus = load_corpus()
    incidents = corpus["incidents"]
    summary = corpus["incident_summary"]

    assert summary["total"] == len(incidents)
    for field in (
        "severity",
        "category",
        "outcome",
        "cause_attribution",
        "one_shot_risk",
    ):
        assert summary[field] == dict(Counter(row[field] for row in incidents))
    assert summary["attributed_model"] == dict(
        Counter(row["model"]["id"] for row in incidents)
    )


def test_corpus_is_redacted_and_contains_no_raw_input_payloads() -> None:
    corpus = load_corpus()
    rendered = json.dumps(corpus, sort_keys=True)
    lowered = rendered.lower()

    forbidden_keys = {
        "typed_text",
        "raw_text",
        "raw_command",
        "screenshot_bytes",
        "credential",
        "password",
        "access_token",
    }

    def walk(value: object) -> None:
        if isinstance(value, dict):
            assert not forbidden_keys.intersection(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(corpus)
    assert not re.search(r"https?://", rendered)
    assert not re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", rendered)
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", rendered)
    assert not re.search(r"\b[A-Za-z0-9+/]{80,}={0,2}\b", rendered)
    assert "ocid1." not in lowered
    assert "vm." not in lowered


def test_every_reviewed_client_has_incident_coverage() -> None:
    corpus = load_corpus()
    clients_with_incidents = {
        incident["source"]["client"] for incident in corpus["incidents"]
    }
    assert clients_with_incidents == {"claude_code", "codex", "opencode"}

    covered_sessions = {
        (incident["source"]["client"], incident["source"]["session_id"])
        for incident in corpus["incidents"]
    }
    for session in corpus["sessions"]:
        key = (session["client"], session["session_id"])
        if "no_reconstructed" in session["coverage"]:
            assert key not in covered_sessions
        else:
            assert key in covered_sessions


@pytest.mark.parametrize("size", [4265, 10259])
def test_historical_runaway_payload_sizes_are_rejected_before_hid(size: int) -> None:
    """Executable guard for the two largest reconstructed typing incidents."""
    with pytest.raises(BurstError, match="max is"):
        validate_actions(
            [{"type": "type_text", "text": "x" * size, "method": "print"}]
        )
