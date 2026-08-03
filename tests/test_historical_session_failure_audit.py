from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = (
    ROOT
    / "bench"
    / "results"
    / "2026-08-03"
    / "safety"
    / "historical-session-failure-audit.json"
)
BASELINE_PATH = ROOT / "bench" / "historical_pikvm_incidents.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_historical_session_audit_reconciles_the_authoritative_corpus() -> None:
    audit = _load(AUDIT_PATH)
    baseline = _load(BASELINE_PATH)
    baseline_bytes = BASELINE_PATH.read_bytes()
    reconciliation = audit["authoritative_corpus_reconciliation"]
    scope = audit["scope"]

    assert audit["schema_version"] == 2
    assert reconciliation["baseline_sha256"] == hashlib.sha256(
        baseline_bytes
    ).hexdigest()
    assert reconciliation["baseline_sessions"] == 24
    assert reconciliation["baseline_pikvm_tool_calls"] == 4_453
    assert reconciliation["baseline_incidents"] == 70
    assert reconciliation["baseline_incident_summary"] == baseline["incident_summary"]
    assert audit["baseline_incidents"] == baseline["incidents"]

    assert scope["sessions"] == 24
    assert scope["pikvm_tool_calls"] == 4_453
    assert scope["raw_direct_calls"] == 4_416
    assert scope["legacy_hidden_operator_calls"] == 37
    assert scope["current_managed_computer_calls"] == 0
    assert (
        scope["raw_direct_calls"]
        + scope["legacy_hidden_operator_calls"]
        + scope["current_managed_computer_calls"]
        == scope["pikvm_tool_calls"]
    )

    expected_clients = {
        "claude_code": (15, 2_876, 2_839, 37),
        "codex": (7, 1_482, 1_482, 0),
        "opencode": (2, 95, 95, 0),
    }
    for client, (sessions, calls, direct, legacy) in expected_clients.items():
        row = scope["clients"][client]
        assert row["sessions"] == sessions
        assert row["pikvm_tool_calls"] == calls
        assert row["raw_direct_calls"] == direct
        assert row["legacy_hidden_operator_calls"] == legacy
        assert row["current_managed_computer_calls"] == 0


def test_historical_session_audit_covers_every_session_and_model_call() -> None:
    audit = _load(AUDIT_PATH)
    baseline = _load(BASELINE_PATH)
    sessions = audit["sessions"]

    session_ids = [session["session_id"] for session in sessions]
    assert len(session_ids) == 24
    assert len(set(session_ids)) == 24
    assert set(session_ids) == {
        session["session_id"] for session in baseline["sessions"]
    }
    assert sum(session["pikvm_tool_calls"] for session in sessions) == 4_453
    assert sum(
        sum(session["model_call_counts"].values()) for session in sessions
    ) == 4_453

    for session in sessions:
        assert session["client"] in {"claude_code", "codex", "opencode"}
        assert session["provider"]
        input_surface = session["input_surface"]
        assert input_surface["current_managed_computer_calls"] == 0
        assert (
            input_surface["raw_direct_calls"]
            + input_surface["legacy_hidden_operator_calls"]
            == session["pikvm_tool_calls"]
        )
        assert session["source_retention"]
        assert session["source_path"] or session["source_retention"] == (
            "normalized authoritative corpus only"
        )
        assert session["uncertainty"]
        assert session["reconciled_failure_status"] in {
            "incidents_reconstructed",
            "supplemental_failure_found",
            "no_reconstructed_failure",
        }

    assert [
        session["session_id"]
        for session in sessions
        if session["reconciled_failure_status"] == "no_reconstructed_failure"
    ] == ["019f942a-8387-7f53-ab13-28bacd39b51a"]


def test_supplemental_call_evidence_is_labelled_and_source_cited() -> None:
    audit = _load(AUDIT_PATH)
    reconciliation = audit["authoritative_corpus_reconciliation"]
    supplemental = audit["supplemental_deep_call_evidence"]
    incidents = [
        incident
        for session in supplemental["sessions"]
        for incident in session["incidents"]
    ]
    evidence = [entry for incident in incidents for entry in incident["evidence"]]

    assert reconciliation["supplemental_deep_review_sessions"] == 12
    assert reconciliation["supplemental_call_id_incident_chains"] == 23
    assert supplemental["incident_chain_count"] == 23
    assert len(supplemental["sessions"]) == 12
    assert len(incidents) == 23
    assert len({incident["id"] for incident in incidents}) == 23
    assert len(evidence) == 77
    assert sum(entry.get("call_id") is not None for entry in evidence) == 48

    for incident in incidents:
        assert incident["danger_class"] in {"P0", "P1", "P2"}
        assert incident["models"]
        assert incident["failure"]
        assert incident["user_correction"]
        assert incident["outcome"]
        assert incident["uncertainty"]
        assert incident["evidence"]
        assert all(entry["timestamp"] for entry in incident["evidence"])

    assert len(audit["p0_one_shot_cases"]) == 3


def test_historical_session_audit_remains_history_only_and_redacted() -> None:
    audit = _load(AUDIT_PATH)
    environment = audit["environment"]
    serialized = json.dumps(audit, sort_keys=True)

    assert environment == {
        "history_only": True,
        "production_pikvm_touched": False,
        "remote_target_contacted": False,
        "raw_typed_payloads_retained": False,
        "screenshots_retained": False,
    }
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert not re.search(r"[A-Za-z0-9+/]{200,}={0,2}", serialized)
    assert "raw_arguments" not in serialized
    assert "screenshot_bytes" not in serialized
