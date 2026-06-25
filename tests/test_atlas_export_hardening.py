"""Regression tests from the GPT-5.5 (Codex) Sprint D review — Atlas redaction.

The exporter must not leak secrets/bodies that arrive via FREE-TEXT (the task or
an error string), not just structured trace fields.
"""

from __future__ import annotations

from pikvm_agent.memory.atlas_export import build_memory_update


def test_operator_error_input_value_is_redacted() -> None:
    # P1.2: a Pydantic error echoing a typed body must not reach Atlas.
    secret = "hunter2 secret body"
    err = f"operator decision failed validation: input_value={{'type': 'bad', 'text': '{secret}'}}, input_type=dict"
    mu = build_memory_update(
        "s", "safe task",
        [{"kind": "operator_error", "error": err}, {"kind": "finalise", "status": "failed"}],
        status="failed",
    )
    assert secret not in mu.markdown
    assert secret not in str(mu.incident)
    assert mu.redacted is True


def test_secret_in_task_is_redacted() -> None:
    # P1.1: structured secrets in the task must be masked everywhere it is echoed.
    mu = build_memory_update("s", "log in with password=hunter2 then continue",
                             [{"kind": "finalise", "status": "done"}])
    assert "hunter2" not in mu.markdown
    assert "hunter2" not in str(mu.incident)
    assert mu.redacted is True


def test_quoted_body_in_task_is_redacted() -> None:
    mu = build_memory_update("s", "send message: \"the confidential rollout details\"",
                             [{"kind": "finalise", "status": "done"}])
    assert "confidential rollout details" not in mu.markdown
    assert mu.redacted is True


def test_benign_task_is_not_flagged() -> None:
    mu = build_memory_update("s", "open the README in VS Code",
                             [{"kind": "observe"}, {"kind": "finalise", "status": "done"}])
    assert "README" in mu.markdown
    assert mu.redacted is False
