"""Phase 8 — Atlas memory export: safety + correctness invariants.

The export turns a trace into a *proposal*; it must never leak screenshot paths,
secrets, credentials, or verbatim typed text into the markdown or incident dict.
"""

from __future__ import annotations

import pikvm_agent.memory.atlas_export as atlas_export
from pikvm_agent.memory import (
    MemoryUpdate,
    build_memory_update,
    memory_slug,
)

SCREENSHOT_PATH = "/home/kieran/.local/share/pikvm/sessions/s1/frame_0007.png"
TYPED_BODY = "Dear board, the merger numbers are confidential and final."
PLANTED_SECRET = "hunter2-SUPERSECRET-token"

TASK = "Compose the quarterly board email in Outlook"


def _trace() -> list[dict]:
    """A trace that includes a screenshot path, a typed body, an approval, a
    policy block, and a decision — plus a planted secret/credential."""
    return [
        {"kind": "session_start", "session_id": "s1", "task": TASK},
        {
            "kind": "observe",
            "session_id": "s1",
            "frame_id": 1,
            "world_version": 1,
            "screenshot_path": SCREENSHOT_PATH,
        },
        {
            "kind": "decision",
            "session_id": "s1",
            "step": 1,
            "intent": "open compose window",
            "actions": ["click_element"],
            "risk": "navigation",
        },
        {
            "kind": "decision",
            "session_id": "s1",
            "step": 2,
            "intent": "type the email body",
            "actions": [{"type": "type_text", "text": TYPED_BODY}],
            "risk": "communication_draft",
        },
        # A credential prompt step carrying secrets that MUST be stripped.
        {
            "kind": "decision",
            "session_id": "s1",
            "step": 3,
            "intent": "authenticate",
            "password": PLANTED_SECRET,
            "token": PLANTED_SECRET,
            "actions": [{"type": "type_text", "text": PLANTED_SECRET}],
            "risk": "credential_entry",
        },
        {
            "kind": "approval_required",
            "session_id": "s1",
            "reason": "communication_send requires approval",
            "risk": "communication_send",
        },
        {"kind": "approved", "session_id": "s1", "approval_id": "a1"},
        {
            "kind": "executed",
            "session_id": "s1",
            "status": "verified",
            "actions": ["click_element"],
        },
        {
            "kind": "policy_block",
            "session_id": "s1",
            "reason": "delete is blocked by policy",
            "mode": "outlook.compose",
        },
        {"kind": "finalise", "session_id": "s1", "status": "done", "steps": 3},
    ]


def test_build_memory_update_redacts_and_summarizes() -> None:
    mu = build_memory_update("s1", TASK, _trace(), status="done")
    assert isinstance(mu, MemoryUpdate)

    blob = mu.markdown + "\n" + str(mu.incident)

    # Task + a summary are present.
    assert TASK in mu.markdown
    assert TASK in str(mu.incident)
    assert mu.incident["summary"]
    assert "decision" in mu.incident["summary"].lower()

    # NONE of the sensitive bodies leak — not the path, not the typed text, not
    # the planted secret (test the distinctive token from each).
    assert SCREENSHOT_PATH not in blob
    assert "frame_0007.png" not in blob
    assert TYPED_BODY not in blob
    assert "confidential and final" not in blob
    assert PLANTED_SECRET not in blob
    assert "SUPERSECRET" not in blob

    # Anything sensitive was present, so the proposal is flagged redacted.
    assert mu.redacted is True


def test_stats_counts_are_correct() -> None:
    mu = build_memory_update("s1", TASK, _trace(), status="done")
    s = mu.stats
    assert s["decisions"] == 3
    assert s["approvals_required"] == 1
    assert s["approvals_approved"] == 1
    assert s["approvals_denied"] == 0
    assert s["executions"] == 1
    assert s["policy_blocks"] == 1
    assert s["observes"] == 1
    # Typed chars are length-summarized from action bodies, never the content.
    assert s["typed_chars"] == len(TYPED_BODY) + len(PLANTED_SECRET)


def test_clean_trace_is_not_flagged_redacted() -> None:
    clean = [
        {"kind": "session_start", "session_id": "s2", "task": "open the readme"},
        {"kind": "decision", "session_id": "s2", "step": 1, "intent": "scroll", "actions": ["scroll"], "risk": "navigation"},
        {"kind": "finalise", "session_id": "s2", "status": "done", "steps": 1},
    ]
    mu = build_memory_update("s2", "open the readme", clean, status="done")
    assert mu.redacted is False
    assert mu.stats["decisions"] == 1


def test_failure_becomes_incident_with_reasons() -> None:
    trace = [
        {"kind": "decision", "session_id": "s3", "step": 1, "intent": "x", "actions": ["click_element"], "risk": "navigation"},
        {"kind": "execute_refused", "session_id": "s3", "reason": "stale_world", "planned": 1, "current": 2},
        {"kind": "policy_block", "session_id": "s3", "reason": "send is blocked"},
        {"kind": "finalise", "session_id": "s3", "status": "blocked", "steps": 1},
    ]
    mu = build_memory_update("s3", "send a message", trace, status="running")
    assert mu.incident["kind"] == "incident"
    assert mu.incident["final_status"] == "blocked"
    assert mu.stats["stale_refusals"] == 1
    assert any("stale" in r for r in mu.incident["failure_reasons"])


def test_keyboard_layout_lesson_surfaces() -> None:
    trace = [
        {"kind": "decision", "session_id": "s4", "step": 1, "intent": "type", "actions": ["type_text"], "risk": "text_entry"},
        {"kind": "recover", "session_id": "s4", "from_status": "running", "mode": "vscode.editor", "result": {"action": "keyboard layout correction"}},
        {"kind": "finalise", "session_id": "s4", "status": "done", "steps": 1},
    ]
    mu = build_memory_update("s4", "type a path", trace, status="done")
    assert mu.stats["keyboard_layout_correction"] is True
    assert any("UK keyboard" in l for l in mu.incident["lessons"])
    assert "UK keyboard" in mu.markdown


def test_memory_slug_is_safe_kebab() -> None:
    slug = memory_slug("Open the README!")
    assert slug == "open-the-readme"
    assert slug == slug.lower()
    assert " " not in slug and "/" not in slug and "!" not in slug
    # Empty / whitespace-only tasks degrade to a safe default.
    assert memory_slug("   ") == "session"
    assert memory_slug("***") == "session"


def test_page_path_under_memory() -> None:
    mu = build_memory_update("s1", TASK, _trace(), status="done")
    assert mu.page_path.startswith("memory/")
    assert mu.page_path.endswith(".md")


def test_redact_keys_cover_required_fields() -> None:
    required = {"screenshot_path", "image_path", "frame_path", "text", "password", "secret", "token"}
    assert required <= atlas_export.REDACT_KEYS
