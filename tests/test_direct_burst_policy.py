from __future__ import annotations

import pytest

from pikvm_agent.config import PolicyConfig
from pikvm_agent.harness.lab import isolated_benchmark_policy
from pikvm_agent.policy.direct import classify_direct_burst


def _classify(actions: list[dict]):
    return classify_direct_burst(actions, PolicyConfig())


def test_safe_grounded_navigation_and_plain_editor_typing_are_allowed() -> None:
    assert _classify(
        [{"type": "click", "observed_target_text": "Search"}]
    ).status == "allowed"
    assert _classify(
        [{"type": "type_text", "text": "A careful paragraph.", "context": "editor"}]
    ).status == "allowed"


def test_dangerous_command_requires_human_even_before_enter() -> None:
    verdict = _classify(
        [{"type": "type_text", "text": "sudo rm -rf /tmp/example", "context": "terminal"}]
    )
    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"


def test_unknown_mutating_terminal_command_requires_human() -> None:
    verdict = _classify(
        [{"type": "type_text", "text": "Set-Content out.txt hello", "context": "terminal"}]
    )
    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"


def test_segmented_read_only_terminal_command_is_classified_as_one_line() -> None:
    verdict = _classify(
        [
            {"type": "type_text", "text": "ffprobe ", "context": "terminal"},
            {"type": "type_text", "text": "video.", "context": "terminal"},
            {"type": "type_text", "text": "mp4", "context": "terminal"},
            {"type": "wait_for_stable_screen", "timeout_ms": 1000},
        ]
    )

    assert verdict.status == "allowed"


def test_isolated_benchmark_allows_bounded_read_only_discovery_commands() -> None:
    verdict = classify_direct_burst(
        [
            {
                "type": "type_text",
                "text": "command -v ffmpeg ffprobe",
                "context": "terminal",
            },
            {"type": "key", "keys": ["Return"]},
            {
                "type": "type_text",
                "text": "find ~ -maxdepth 3 -name 'video.mp4' -print",
                "context": "terminal",
            },
            {"type": "key", "keys": ["Return"]},
        ],
        isolated_benchmark_policy(),
    )

    assert verdict.status == "allowed"


def test_segmented_dangerous_terminal_command_still_requires_human() -> None:
    verdict = _classify(
        [
            {"type": "type_text", "text": "rm ", "context": "terminal"},
            {"type": "type_text", "text": "-rf ", "context": "terminal"},
            {"type": "type_text", "text": "/tmp/example", "context": "terminal"},
        ]
    )

    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"


def test_run_dialog_shell_command_is_inferred_without_model_context() -> None:
    verdict = _classify(
        [
            {"type": "key", "keys": ["WIN", "R"]},
            {
                "type": "type_text",
                "text": (
                    "powershell -NoP -C \"Stop-Process -Name observer -Force; "
                    "Start-Process C:\\PiKVM-Harness\\observer.exe\""
                ),
            },
        ]
    )

    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"


def test_shell_launcher_is_inferred_without_context_metadata() -> None:
    verdict = _classify(
        [{"type": "type_text", "text": "cmd /c mkdir C:\\temporary-fixture"}]
    )

    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"


def test_observed_send_and_delete_targets_require_human() -> None:
    send = _classify([{"type": "click", "observed_target_text": "Send message"}])
    delete = _classify([{"type": "click", "target_text": "Delete record"}])
    assert (send.status, send.category) == ("approval_required", "communication_send")
    assert (delete.status, delete.category) == ("approval_required", "delete")


@pytest.mark.parametrize("label", ["Replace", "Replace All", "Replace in files"])
def test_bulk_replacement_requires_human_review(label: str) -> None:
    verdict = _classify(
        [{"type": "click", "observed_target_text": label}]
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "local_file_edit",
    )


def test_ocr_corruption_cannot_bypass_bulk_replacement_gate() -> None:
    verdict = _classify(
        [{"type": "click", "observed_target_text": "RepIace AII"}]
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "local_file_edit",
    )


def test_purchase_and_permissions_require_human() -> None:
    purchase = _classify([{"type": "click", "target_text": "Place order"}])
    permissions = _classify([{"type": "click", "target_text": "Grant admin access"}])
    assert purchase.category == "financial_or_purchase"
    assert permissions.category == "account_or_permission_change"


def test_ungrounded_coordinate_click_fails_closed() -> None:
    verdict = _classify(
        [{"type": "click", "x": 100, "y": 200, "target_text": "Search"}]
    )

    assert verdict.status == "approval_required"
    assert verdict.category == "unknown"


def test_credentials_consent_and_generic_submit_require_human() -> None:
    credential = _classify(
        [{"type": "click", "observed_target_text": "Sign in"}]
    )
    consent = _classify(
        [{"type": "click", "observed_target_text": "Accept terms"}]
    )
    submit = _classify(
        [{"type": "click", "observed_target_text": "Submit form"}]
    )

    assert credential.category == "credential_entry"
    assert consent.category == "legal_or_consent"
    assert submit.category == "communication_send"


def test_policy_always_block_wins() -> None:
    policy = PolicyConfig(always_block=["delete"])
    verdict = classify_direct_burst(
        [{"type": "click", "target_text": "Delete record"}], policy
    )
    assert verdict.status == "blocked"


def test_commit_shortcuts_require_human_review() -> None:
    save = _classify([{"type": "key", "keys": ["CTRL", "S"]}])
    send = _classify([{"type": "key", "keys": ["CTRL", "ENTER"]}])
    outlook_send = _classify(
        [{"type": "key", "keys": ["AltLeft", "KeyS"]}]
    )

    assert (save.status, save.category) == (
        "approval_required",
        "local_file_edit",
    )
    assert (send.status, send.category) == (
        "approval_required",
        "communication_send",
    )
    assert (outlook_send.status, outlook_send.category) == (
        "approval_required",
        "communication_send",
    )


def test_file_cut_and_paste_are_local_edits_in_normal_policy() -> None:
    cut = _classify([{"type": "key", "keys": ["CTRL", "X"]}])
    paste = _classify([{"type": "key", "keys": ["CTRL", "V"]}])

    assert (cut.status, cut.category) == (
        "approval_required",
        "local_file_edit",
    )
    assert (paste.status, paste.category) == (
        "approval_required",
        "local_file_edit",
    )


@pytest.mark.parametrize("key", ["Z", "Y"])
def test_undo_and_redo_shortcuts_require_human_review(key: str) -> None:
    verdict = _classify([{"type": "key", "keys": ["CTRL", key]}])

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "local_file_edit",
    )


def test_isolated_benchmark_explicitly_allows_reversible_cut_and_paste() -> None:
    verdict = classify_direct_burst(
        [{"type": "key", "keys": ["CTRL", "X"]}],
        isolated_benchmark_policy(),
    )

    assert (verdict.status, verdict.category) == (
        "allowed",
        "local_file_edit",
    )


def test_bare_enter_fails_closed_because_it_can_submit_the_focused_surface() -> None:
    verdict = _classify([{"type": "key", "keys": ["ENTER"]}])

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "unknown",
    )
    assert "commit" in verdict.reason


def test_read_only_terminal_text_does_not_bypass_enter_focus_gate() -> None:
    verdict = _classify(
        [
            {
                "type": "type_text",
                "text": "ffprobe video.mp4",
                "context": "terminal",
            },
            {"type": "key", "keys": ["ENTER"]},
        ]
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "unknown",
    )


@pytest.mark.parametrize(
    ("label", "category"),
    [
        ("Forward email", "communication_send"),
        ("Join meeting", "communication_send"),
        ("Start call", "communication_send"),
        ("Upload file", "file_external_upload"),
        ("Donate now", "financial_or_purchase"),
        ("Transfer funds", "financial_or_purchase"),
        ("Renew subscription", "financial_or_purchase"),
        ("Add member", "account_or_permission_change"),
        ("Remove member", "account_or_permission_change"),
        ("Transfer ownership", "account_or_permission_change"),
        ("Allow access", "account_or_permission_change"),
        ("Approve request", "account_or_permission_change"),
        ("Empty recycle bin", "delete"),
        ("Discard changes", "delete"),
        ("Restart now", "power_or_firmware"),
        ("Shut down", "power_or_firmware"),
        ("Factory reset", "power_or_firmware"),
        ("Save", "local_file_edit"),
        ("Rename file", "local_file_edit"),
        ("Overwrite existing", "local_file_edit"),
        ("Apply changes", "system_setting_change"),
        ("Enable feature", "system_setting_change"),
        ("OK", "unknown"),
        ("OK to continue", "unknown"),
        ("Continue", "unknown"),
        ("Done", "unknown"),
    ],
)
def test_real_world_commit_labels_require_human(
    label: str,
    category: str,
) -> None:
    verdict = _classify(
        [{"type": "click", "observed_target_text": label}]
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        category,
    )


@pytest.mark.parametrize(
    ("ocr_label", "category"),
    [
        ("Senci message", "communication_send"),
        ("DeIete record", "delete"),
        ("Purcnase now", "financial_or_purchase"),
        ("Grarit admin access", "account_or_permission_change"),
        ("lnstaII update", "software_installation"),
    ],
)
def test_common_ocr_corruption_cannot_bypass_dangerous_commit_gate(
    ocr_label: str,
    category: str,
) -> None:
    verdict = _classify(
        [{"type": "click", "observed_target_text": ocr_label}]
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        category,
    )


@pytest.mark.parametrize(
    "label",
    [
        "Search",
        "Open",
        "Cancel",
        "Back",
        "Copy",
        "Download",
        "View",
        "Preview",
        "Learn more",
        "Settings",
        "Help",
        "Find and Replace",
    ],
)
def test_routine_navigation_is_not_a_fuzzy_false_positive(label: str) -> None:
    verdict = _classify(
        [{"type": "click", "observed_target_text": label}]
    )

    assert verdict.status == "allowed"


@pytest.mark.parametrize(
    "label",
    [
        "Disable security",
        "Turn off firewall",
        "Disable antivirus",
        "Turn off Defender",
        "Disable protection",
        "Bypass protection",
    ],
)
def test_security_disable_controls_are_blocked_not_merely_approved(
    label: str,
) -> None:
    verdict = _classify(
        [{"type": "click", "observed_target_text": label}]
    )

    assert (verdict.status, verdict.category) == (
        "blocked",
        "disable_security",
    )
