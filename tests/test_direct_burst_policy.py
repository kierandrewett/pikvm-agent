from __future__ import annotations

import pytest

from pikvm_agent.config import PolicyConfig
from pikvm_agent.harness.lab import isolated_benchmark_policy
from pikvm_agent.policy.direct import (
    classify_direct_burst,
    is_confirmed_local_file_overwrite_surface,
    is_confirmed_safe_windows_error_dismissal,
    is_confirmed_file_explorer_surface,
    is_confirmed_windows_run_surface,
    is_safe_local_navigation_target,
)


def _classify(actions: list[dict]):
    return classify_direct_burst(actions, PolicyConfig())


def test_safe_grounded_navigation_and_plain_editor_typing_are_allowed() -> None:
    assert _classify(
        [{"type": "click", "observed_target_text": "Search"}]
    ).status == "allowed"
    assert _classify(
        [{"type": "type_text", "text": "A careful paragraph.", "context": "editor"}]
    ).status == "allowed"


def test_spreadsheet_grid_requires_one_local_file_edit_approval() -> None:
    verdict = _classify(
        [
            {
                "type": "spreadsheet_grid",
                "rows": [["Q1", "124.8"], ["Q2", "132.1"]],
            }
        ]
    )

    assert (verdict.status, verdict.category, verdict.level) == (
        "approval_required",
        "local_file_edit",
        "medium",
    )


def test_dangerous_command_requires_human_even_before_enter() -> None:
    verdict = _classify(
        [{"type": "type_text", "text": "sudo rm -rf /tmp/example", "context": "terminal"}]
    )
    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"


@pytest.mark.parametrize(
    "command",
    [
        "TF_AUTO_APPROVE=1 ./scripts/01-bootstrap-runner.sh",
        "terraform apply -auto-approve",
        "terraform destroy --auto-approve",
        "oci compute instance terminate --instance-id example --force",
    ],
)
def test_infrastructure_auto_approval_requires_human_without_context_metadata(
    command: str,
) -> None:
    verdict = _classify([{"type": "type_text", "text": command}])

    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"
    assert verdict.level == "high"


def test_unknown_mutating_terminal_command_requires_human() -> None:
    verdict = _classify(
        [{"type": "type_text", "text": "Set-Content out.txt hello", "context": "terminal"}]
    )
    assert verdict.status == "approval_required"
    assert verdict.category == "terminal_mutating"


def test_terminal_system_setting_uses_the_specific_policy_category() -> None:
    actions = [
        {
            "type": "type_text",
            "text": (
                "gsettings set "
                "org.gnome.settings-daemon.plugins.power idle-dim false"
            ),
            "context": "terminal",
        }
    ]

    normal = _classify(actions)
    benchmark = classify_direct_burst(
        actions,
        isolated_benchmark_policy(),
    )

    assert (normal.status, normal.category, normal.level) == (
        "approval_required",
        "system_setting_change",
        "medium",
    )
    assert (benchmark.status, benchmark.category, benchmark.level) == (
        "allowed",
        "system_setting_change",
        "medium",
    )


def test_terminal_gsettings_observation_does_not_require_approval() -> None:
    for verb_and_arguments in (
        "get org.gnome.settings-daemon.plugins.power idle-dim",
        "range org.gnome.desktop.session idle-delay",
        "describe org.gnome.desktop.session idle-delay",
        "list-schemas",
        "list-relocatable-schemas",
        "list-keys org.gnome.desktop.session",
        "list-children org.gnome.desktop",
        "list-recursively org.gnome.desktop.session",
        "writable org.gnome.desktop.session idle-delay",
    ):
        verdict = _classify(
            [
                {
                    "type": "type_text",
                    "text": f"gsettings {verb_and_arguments}",
                    "context": "terminal",
                }
            ]
        )
        assert verdict.status == "allowed", verb_and_arguments


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


@pytest.mark.parametrize(
    "target",
    [
        "ms-settings:about",
        "ms-settings:display",
        "notepad",
        "explorer",
        "explorer.exe",
    ],
)
def test_verified_windows_run_launch_is_routine_local_navigation(
    target: str,
) -> None:
    verdict = _classify(
        [
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "wait", "ms": 500},
            {
                "type": "type_text",
                "text": target,
                "context": "field",
                "verification": "exact",
            },
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_stable_screen", "timeout_ms": 8000},
        ]
    )

    assert verdict.status == "allowed"


def test_windows_run_focus_preflight_remains_routine_local_navigation() -> None:
    verdict = _classify(
        [
            {"type": "key", "keys": ["ESC"]},
            {"type": "wait", "ms": 250},
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "wait_for_change", "timeout_ms": 5_000},
            {
                "type": "type_text",
                "text": "calc",
                "context": "field",
                "verification": "exact",
            },
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_stable_screen", "timeout_ms": 8_000},
        ]
    )

    assert verdict.status == "allowed"


@pytest.mark.parametrize(
    ("actions", "expected_category"),
    [
        (
            [
                {
                    "type": "type_text",
                    "text": "ms-settings:about",
                    "context": "field",
                    "verification": "exact",
                },
                {"type": "key", "keys": ["ENTER"]},
            ],
            "unknown",
        ),
        (
            [
                {"type": "key", "keys": ["WIN", "R"]},
                {
                    "type": "type_text",
                    "text": "ms-settings:about & cmd",
                    "context": "field",
                    "verification": "exact",
                },
                {"type": "key", "keys": ["ENTER"]},
            ],
            "terminal_mutating",
        ),
        (
            [
                {"type": "key", "keys": ["WIN", "R"]},
                {
                    "type": "type_text",
                    "text": "explorer.exe shell:MyComputerFolder",
                    "context": "field",
                    "verification": "exact",
                },
                {"type": "key", "keys": ["ENTER"]},
            ],
            "terminal_mutating",
        ),
        (
            [
                {"type": "key", "keys": ["WIN", "R"]},
                {
                    "type": "type_text",
                    "text": "ms-settings:about",
                    "context": "terminal",
                    "verification": "exact",
                },
                {"type": "key", "keys": ["ENTER"]},
            ],
            "terminal_mutating",
        ),
    ],
)
def test_windows_run_near_misses_still_require_human(
    actions: list[dict],
    expected_category: str,
) -> None:
    verdict = _classify(actions)

    assert verdict.status == "approval_required"
    assert verdict.category == expected_category


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


def test_shift_enter_remains_a_non_submitting_editor_line_break() -> None:
    verdict = _classify(
        [{"type": "key", "keys": ["SHIFT", "ENTER"]}]
    )

    assert verdict.status == "allowed"


def test_numpad_enter_also_fails_closed_on_an_unknown_surface() -> None:
    verdict = _classify([{"type": "key", "keys": ["NumpadEnter"]}])

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "unknown",
    )


def test_verified_local_navigation_commit_requires_explicit_grounding() -> None:
    actions = [
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_change", "timeout_ms": 5000},
    ]

    assert _classify(actions).status == "approval_required"
    assert classify_direct_burst(
        actions,
        PolicyConfig(),
        verified_local_navigation_commit=True,
    ).status == "allowed"


def test_windows_run_surface_accepts_real_ocr_noise_with_same_frame_draft() -> None:
    dialog_text = (
        "Run x Type the name of 8 program, folder, document or Internet "
        "resource, and Windows will open i for you. Open: TEE "
        "OK Cancel Browse"
    )

    assert is_confirmed_windows_run_surface(
        "Windows desktop",
        draft_text="notepad",
        dialog_text=dialog_text,
        verified_same_frame_draft=True,
    )
    assert not is_confirmed_windows_run_surface(
        "Windows desktop",
        draft_text="notepad",
        dialog_text=dialog_text,
    )


def test_windows_run_surface_rejects_message_compose_lookalike() -> None:
    assert not is_confirmed_windows_run_surface(
        "New message  Send",
        draft_text="notepad",
        dialog_text=(
            "Type the name of a program, folder, document or Internet "
            "resource, and Windows will open it for you. Open OK Cancel Browse"
        ),
        verified_same_frame_draft=True,
    )


def test_file_explorer_surface_requires_multiple_independent_markers() -> None:
    assert is_confirmed_file_explorer_surface(
        "Home  This PC  Search Home  Quick access  Downloads"
    )
    assert not is_confirmed_file_explorer_surface(
        "Home  Search Home  Quick access  Downloads  Documents"
    )
    assert not is_confirmed_file_explorer_surface(
        "New message  This PC  Send"
    )


def test_save_as_navigation_requires_safe_path_and_top_band_evidence() -> None:
    path = r"C:\PiKVM-Harness\workspace\codex-50"
    surface = (
        "Save as  New folder  File name: text-01.txt  "
        "Save as type: Text documents"
    )

    assert is_safe_local_navigation_target(path)
    assert not is_safe_local_navigation_target(r"C:\workspace\..\Windows")
    assert not is_safe_local_navigation_target(
        r"C:\workspace\codex-50\*.txt"
    )
    assert is_confirmed_file_explorer_surface(
        surface,
        draft_text=path,
        top_band_text=path,
    )
    assert not is_confirmed_file_explorer_surface(
        surface,
        draft_text=path,
        top_band_text="Documents",
    )
    assert not is_confirmed_file_explorer_surface(
        f"New message {path} Send",
        draft_text=path,
        top_band_text=path,
    )


def test_same_exact_frame_tolerates_real_save_as_ocr_noise() -> None:
    path = r"C:\PiKVM-Harness\workspace\codex-50"
    noisy_surface = (
        "Reosie BD Swveas Organise New folder "
        "Reluble sutomation starts with batt"
    )
    noisy_top_band = (
        r"BD Seve as > Y BB C:\PIKVM-Hamess\workspace\codex-50 "
        "Search Documents Organise New folder"
    )

    assert is_confirmed_file_explorer_surface(
        noisy_surface,
        draft_text=path,
        top_band_text=noisy_top_band,
        verified_same_frame_draft=True,
    )
    assert not is_confirmed_file_explorer_surface(
        noisy_surface,
        draft_text=path,
        top_band_text=noisy_top_band,
    )
    assert not is_confirmed_file_explorer_surface(
        f"New message {path} Send",
        draft_text=path,
        top_band_text=noisy_top_band,
        verified_same_frame_draft=True,
    )


def test_calculator_expression_requires_independent_surface_evidence() -> None:
    actions = [
        {"type": "key", "keys": ["Digit3"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit9"]},
        {"type": "key", "keys": ["Enter"]},
    ]

    ungrounded = _classify(actions)
    grounded = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=(
            "Standard  History  Memory  There's no history yet."
        ),
    )

    assert (ungrounded.status, ungrounded.category) == (
        "approval_required",
        "unknown",
    )
    assert grounded.status == "allowed"


def test_grounded_decimal_calculator_expression_is_allowed() -> None:
    actions = [
        {"type": "key", "keys": ["Digit8"]},
        {"type": "key", "keys": ["Digit6"]},
        {"type": "key", "keys": ["Digit4"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadDecimal"]},
        {"type": "key", "keys": ["Digit5"]},
        {"type": "key", "keys": ["NumpadDivide"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit0"]},
        {"type": "key", "keys": ["Digit0"]},
        {"type": "key", "keys": ["Enter"]},
    ]

    grounded = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=(
            "Standard  History  Memory  There's no history yet."
        ),
    )

    assert grounded.status == "allowed"


def test_arithmetic_looking_message_send_is_not_a_calculator_exception() -> None:
    actions = [
        {"type": "key", "keys": ["Digit3"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit9"]},
        {"type": "key", "keys": ["Enter"]},
    ]

    verdict = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text="New message  To  Subject  Send",
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "unknown",
    )


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


def test_noisy_ok_is_allowed_only_for_confirmed_file_explorer_not_found_error() -> None:
    actions = [
        {"type": "click", "observed_target_text": "(ox"},
        {"type": "wait_for_change", "timeout_ms": 3000},
    ]
    surface = (
        "File Explorer\n"
        "Viindows can’t find "
        r"'C:\PiKVM-Harness\workspace\codex-50'. "
        "Check the spelling and try again"
    )

    assert is_confirmed_safe_windows_error_dismissal(actions, surface)
    assert classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=surface,
    ).status == "allowed"


@pytest.mark.parametrize(
    "surface",
    [
        "Microsoft Teams New message Send",
        "Confirm purchase Checkout Pay now",
        "Windows Security Allow access",
        "File Explorer Are you sure you want to delete this file?",
        "File Explorer Windows can't find the item.",
    ],
)
def test_noisy_ok_stays_gated_without_complete_safe_error_evidence(
    surface: str,
) -> None:
    actions = [{"type": "click", "observed_target_text": "(ox"}]

    assert not is_confirmed_safe_windows_error_dismissal(actions, surface)
    verdict = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=surface,
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "unknown",
    )


def test_bare_enter_on_confirmed_save_as_replacement_is_a_local_edit() -> None:
    actions = [
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_change", "timeout_ms": 2000},
    ]
    surface = (
        "Confirm Save As\n"
        "text-01.txt already exists.\n"
        "Do you want to replace it?\n"
        "Yes No"
    )

    assert is_confirmed_local_file_overwrite_surface(actions, surface)
    verdict = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=surface,
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "local_file_edit",
    )


def test_grounded_yes_on_confirmed_save_as_replacement_is_a_local_edit() -> None:
    actions = [
        {
            "type": "click",
            "x": 672,
            "y": 391,
            "observed_target_text": "Yes",
        },
        {"type": "wait_for_change", "timeout_ms": 3_000},
    ]
    surface = (
        "Confirm Save As\n"
        "text-03.txt already exists.\n"
        "Do you want to replace it?\n"
        "Yes No"
    )

    assert is_confirmed_local_file_overwrite_surface(actions, surface)
    verdict = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=surface,
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "local_file_edit",
    )


@pytest.mark.parametrize(
    "surface",
    [
        "Confirm purchase Pay now Yes No",
        "Microsoft Teams Send message Yes No",
        "File Explorer Delete this file? Yes No",
    ],
)
def test_grounded_yes_stays_unknown_without_complete_overwrite_evidence(
    surface: str,
) -> None:
    actions = [
        {
            "type": "click",
            "x": 672,
            "y": 391,
            "observed_target_text": "Yes",
        }
    ]

    assert not is_confirmed_local_file_overwrite_surface(actions, surface)
    verdict = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=surface,
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "unknown",
    )


def test_measured_save_as_ocr_noise_still_identifies_local_replacement() -> None:
    actions = [{"type": "key", "keys": ["ENTER"]}]
    surface = (
        "Confirm Save As\n"
        "text-O1.otalreadycasts.\n"
        "Do you want to replace at?\n"
        "Yes No"
    )

    assert is_confirmed_local_file_overwrite_surface(actions, surface)
    verdict = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=surface,
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "local_file_edit",
    )


@pytest.mark.parametrize(
    "surface",
    [
        "Confirm purchase Pay now Yes No",
        "Microsoft Teams Send message Yes No",
        "File Explorer Delete this file? Yes No",
        "Confirm Save As Do you want to replace it? Yes No",
    ],
)
def test_bare_enter_stays_unknown_without_complete_overwrite_evidence(
    surface: str,
) -> None:
    actions = [{"type": "key", "keys": ["ENTER"]}]

    assert not is_confirmed_local_file_overwrite_surface(actions, surface)
    verdict = classify_direct_burst(
        actions,
        PolicyConfig(),
        observed_surface_text=surface,
    )

    assert (verdict.status, verdict.category) == (
        "approval_required",
        "unknown",
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
