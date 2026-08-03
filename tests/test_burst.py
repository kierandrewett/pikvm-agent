"""PiKVM HID burst engine — key mapping, dispatch, mid-burst interruption."""

from __future__ import annotations

import hashlib

import pytest

from pikvm_agent.executor.burst import (
    MAX_BURST_TYPE_TEXT_CHARS,
    MAX_TYPE_TEXT_CHARS,
    BurstError,
    needs_post_action_settle,
    normalize_keys,
    recommended_runtime_ms,
    run_burst,
)
from pikvm_agent.pikvm.fake import FakeBackend


def test_normalize_keys_friendly_and_passthrough() -> None:
    assert normalize_keys(["CTRL", "P"]) == ["ControlLeft", "KeyP"]
    assert normalize_keys(["ctrl", "shift", "k"]) == ["ControlLeft", "ShiftLeft", "KeyK"]
    assert normalize_keys(["META"]) == ["MetaLeft"]
    assert normalize_keys(["ENTER"]) == ["Enter"]
    assert normalize_keys(["F11"]) == ["F11"]
    assert normalize_keys(["5"]) == ["Digit5"]
    assert normalize_keys(["*"]) == ["NumpadMultiply"]
    assert normalize_keys(["+"]) == ["NumpadAdd"]
    assert normalize_keys(["/"]) == ["NumpadDivide"]
    assert normalize_keys(["ctrl+End"]) == ["ControlLeft", "End"]
    assert normalize_keys(["Ctrl + Shift + S"]) == [
        "ControlLeft",
        "ShiftLeft",
        "KeyS",
    ]
    # already-valid PiKVM codes pass straight through
    assert normalize_keys(["ControlLeft", "KeyA"]) == ["ControlLeft", "KeyA"]


def test_normalize_keys_rejects_unknown_multicharacter_tokens() -> None:
    with pytest.raises(BurstError, match="unsupported key token"):
        normalize_keys(["ctrl+DefinitelyNotAKey"])


async def test_invalid_key_token_rejects_whole_burst_before_hid() -> None:
    be = FakeBackend()

    with pytest.raises(BurstError, match="unsupported key token"):
        await run_burst(
            [
                {"type": "key", "keys": ["KeyA"]},
                {"type": "key", "keys": ["ctrl+DefinitelyNotAKey"]},
            ],
            backend=be,
        )

    assert not any(method == "keypress" for method, _ in be.calls)


def test_post_action_settle_is_automatic_unless_controller_already_waited() -> None:
    assert needs_post_action_settle([{"type": "click", "x": 100, "y": 200}])
    assert needs_post_action_settle(
        [{"type": "key", "keys": ["ENTER"]}, {"type": "wait", "ms": 100}]
    )
    assert not needs_post_action_settle(
        [{"type": "click", "x": 100, "y": 200}, {"type": "wait", "ms": 500}]
    )
    assert not needs_post_action_settle(
        [
            {"type": "click", "x": 100, "y": 200},
            {"type": "wait_for_stable_screen", "stable_ms": 300},
        ]
    )
    assert not needs_post_action_settle([{"type": "wait", "ms": 500}])


def test_auto_runtime_budget_covers_verified_humanized_typing() -> None:
    actions = [
        {"type": "key", "keys": ["CTRL", "A"]},
        {"type": "type_text", "text": "dim screen when inactive"},
    ]

    budget = recommended_runtime_ms(actions)

    # The old fixed 4s default truncated this exact OSWorld action after
    # "dim screen when". Auto-budget enough time for human cadence plus OCR.
    assert budget >= 15_000


def test_auto_runtime_budget_counts_declared_waits_but_stays_bounded() -> None:
    actions = [
        {"type": "wait_for_change", "timeout_ms": 20_000},
        {"type": "type_text", "text": "x" * MAX_TYPE_TEXT_CHARS, "code": True},
    ]

    budget = recommended_runtime_ms(actions)

    assert budget >= 100_000
    assert budget <= 110_000


def test_auto_runtime_budget_counts_implicit_screen_wait_defaults() -> None:
    assert recommended_runtime_ms([{"type": "wait_for_stable_screen"}]) >= 5_500
    assert recommended_runtime_ms([{"type": "wait_for_change"}]) >= 12_000


def test_auto_runtime_budget_covers_a_cold_exact_ocr_worker() -> None:
    actions = [
        {"type": "key", "keys": ["WIN", "R"]},
        {"type": "wait", "ms": 500},
        {
            "type": "type_text",
            "text": "ms-settings:about",
            "verification": "exact",
        },
        {"type": "key", "keys": ["ENTER"]},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 1200,
            "timeout_ms": 8000,
        },
    ]

    assert recommended_runtime_ms(actions) >= 40_000


def test_auto_runtime_budget_covers_exact_editor_readback() -> None:
    actions = [
        {
            "type": "type_text",
            "text": "Reliable automation starts with observable evidence.",
            "verification": "exact",
            "context": "editor",
        },
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]

    assert recommended_runtime_ms(actions) >= 60_000


def test_auto_runtime_budget_charges_one_later_verifier_not_inline_ocr_per_line(
) -> None:
    actions = [
        {
            "type": "type_text",
            "text": line,
            "code": True,
            "context": "editor",
            "verification": "deferred_exact",
        }
        for line in ("@echo off", "ping -n 1 127.0.0.1 >nul", "exit /b 0")
    ]

    assert recommended_runtime_ms(actions) < 60_000


def test_auto_runtime_budget_counts_spreadsheet_grid_cells() -> None:
    rows = [
        [f"Q{row}", f"{120 + row}.8", f"={row + 2}*1.1", "Reviewed"]
        for row in range(1, 7)
    ]

    budget = recommended_runtime_ms(
        [{"type": "spreadsheet_grid", "rows": rows}]
    )

    assert budget >= 50_000
    assert needs_post_action_settle(
        [{"type": "spreadsheet_grid", "rows": rows}]
    )


async def test_run_burst_executes_in_order() -> None:
    be = FakeBackend()
    actions = [
        {"type": "key", "keys": ["CTRL", "P"]},
        {"type": "wait", "ms": 1},
        {"type": "type_text", "text": "readme.md", "method": "print"},
        {"type": "key", "keys": ["ENTER"]},
        {"type": "click", "x": 100, "y": 200},
        {"type": "scroll", "direction": "down", "amount": 3},
    ]
    out = await run_burst(actions, backend=be)
    assert out.status == "completed"
    assert out.completed == out.total == 6
    methods = [m for m, _ in be.calls]
    assert "keypress" in methods and "print_text" in methods and "click" in methods and "scroll" in methods
    # Ctrl+P mapped to PiKVM codes
    kp = next(kw for m, kw in be.calls if m == "keypress")
    assert kw_keys(kp) == ["ControlLeft", "KeyP"]
    receipt = out.action_receipts[0]
    assert receipt["index"] == 2
    assert receipt["proof_state"] == "issued_only"
    assert receipt["requested_sha256"] == hashlib.sha256(
        b"readme.md"
    ).hexdigest()
    assert receipt["issued_prefix_sha256"] == receipt["requested_sha256"]
    assert receipt["exact_readback_sha256_match"] is False


async def test_wait_for_change_uses_the_frame_before_the_input() -> None:
    class TransitionBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.screenshot_calls = 0

        async def screenshot(self, region=None):
            self.screenshot_calls += 1
            return await super().screenshot(region)

        async def keypress(self, keys: list[str]) -> None:
            await super().keypress(keys)
            self.set_screen("Run dialog")

    backend = TransitionBackend()

    result = await run_burst(
        [
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "wait_for_change", "timeout_ms": 1},
        ],
        backend=backend,
    )

    assert result.status == "completed"
    assert backend.screenshot_calls == 2


async def test_spreadsheet_grid_enters_rows_from_the_active_cell() -> None:
    backend = FakeBackend()

    result = await run_burst(
        [
            {
                "type": "spreadsheet_grid",
                "rows": [["Q1", "124.8"], ["Q2", "132.1"]],
            }
        ],
        backend=backend,
    )

    assert result.status == "completed"
    assert backend.calls == [
        ("type_text", {"text": "Q1", "code": True, "secret": False}),
        ("keypress", {"keys": ["Tab"]}),
        ("type_text", {"text": "124.8", "code": True, "secret": False}),
        ("keypress", {"keys": ["Enter"]}),
        ("keypress", {"keys": ["Home"]}),
        ("type_text", {"text": "Q2", "code": True, "secret": False}),
        ("keypress", {"keys": ["Tab"]}),
        ("type_text", {"text": "132.1", "code": True, "secret": False}),
        ("keypress", {"keys": ["Enter"]}),
    ]
    assert result.action_receipts == [
        {
            "index": 0,
            "type": "spreadsheet_grid",
            "status": "delivered_unverified",
            "verdict": "unverified",
            "proof_state": "issued_only",
            "focus_evidence": "read_back_unavailable",
            "requested_cells": 4,
            "issued_cells": 4,
            "requested_characters": 14,
            "issued_characters": 14,
            "emitted_characters": 14,
            "emitted_exactly_once": True,
            "requested_sha256": hashlib.sha256(
                b"Q1\t124.8\nQ2\t132.1"
            ).hexdigest(),
            "issued_prefix_sha256": hashlib.sha256(
                b"Q1\t124.8\nQ2\t132.1"
            ).hexdigest(),
            "emitted_sha256": hashlib.sha256(
                b"Q1\t124.8\nQ2\t132.1"
            ).hexdigest(),
        }
    ]


async def test_spreadsheet_grid_rejects_ragged_rows_before_input() -> None:
    backend = FakeBackend()

    with pytest.raises(BurstError, match="same number of columns"):
        await run_burst(
            [
                {
                    "type": "spreadsheet_grid",
                    "rows": [["Q1", "124.8"], ["Q2"]],
                }
            ],
            backend=backend,
        )

    assert backend.calls == []


async def test_spreadsheet_grid_rejects_unverified_focus_action_in_same_burst() -> None:
    backend = FakeBackend()

    with pytest.raises(BurstError, match="separate verified focus action"):
        await run_burst(
            [
                {"type": "click", "x": 160, "y": 240},
                {
                    "type": "spreadsheet_grid",
                    "rows": [["Q1", "124.8"]],
                },
            ],
            backend=backend,
        )

    assert backend.calls == []


async def test_spreadsheet_grid_rejects_more_than_eight_rows_before_input() -> None:
    backend = FakeBackend()

    with pytest.raises(BurstError, match="1 to 8 rows"):
        await run_burst(
            [
                {
                    "type": "spreadsheet_grid",
                    "rows": [[f"Q{index}"] for index in range(1, 10)],
                }
            ],
            backend=backend,
        )

    assert backend.calls == []


async def test_spreadsheet_grid_stops_before_the_next_cell_when_control_changes() -> None:
    backend = FakeBackend()
    checks = 0

    def should_continue() -> bool:
        nonlocal checks
        checks += 1
        return checks <= 3

    result = await run_burst(
        [
            {
                "type": "spreadsheet_grid",
                "rows": [["Q1", "124.8"]],
            }
        ],
        backend=backend,
        should_continue=should_continue,
    )

    assert result.status == "interrupted"
    assert result.partial_action == {
        "type": "spreadsheet_grid",
        "issued_cells": 1,
        "requested_cells": 2,
    }
    assert backend.calls == [
        ("type_text", {"text": "Q1", "code": True, "secret": False}),
        ("keypress", {"keys": ["Tab"]}),
        ("release_all", {}),
    ]


async def test_spreadsheet_grid_rejects_control_characters_before_input() -> None:
    backend = FakeBackend()

    with pytest.raises(BurstError, match="control characters"):
        await run_burst(
            [
                {
                    "type": "spreadsheet_grid",
                    "rows": [["Q1\t124.8"]],
                }
            ],
            backend=backend,
        )

    assert backend.calls == []


def kw_keys(kw):
    return kw.get("keys")


async def test_burst_stops_mid_sequence_on_control_change() -> None:
    be = FakeBackend()
    n = {"i": 0}

    def gate() -> bool:
        n["i"] += 1
        return n["i"] <= 2  # allow the first two action-checks, then revoke

    actions = [
        {"type": "key", "keys": ["KeyA"]},
        {"type": "key", "keys": ["KeyB"]},
        {"type": "key", "keys": ["KeyC"]},  # should NOT run
    ]
    out = await run_burst(actions, backend=be, should_continue=gate)
    assert out.status == "interrupted" and out.reason == "control_changed"
    assert out.completed == 2 and out.remaining == 1
    pressed = [kw["keys"] for m, kw in be.calls if m == "keypress"]
    assert pressed == [["KeyA"], ["KeyB"]]  # the third never fired
    assert any(m == "release_all" for m, _ in be.calls)


async def test_burst_deadline_stops_before_next_action() -> None:
    be = FakeBackend()
    # deadline already in the past -> nothing runs.
    out = await run_burst([{"type": "key", "keys": ["KeyA"]}], backend=be, deadline_ms=1.0)
    assert out.status == "interrupted" and out.reason == "deadline" and out.completed == 0


async def test_burst_unknown_action_raises() -> None:
    be = FakeBackend()
    try:
        await run_burst([{"type": "frobnicate"}], backend=be)
        assert False, "expected BurstError"
    except BurstError:
        pass


async def test_burst_rejects_oversized_type_text_before_hid() -> None:
    be = FakeBackend()
    too_long = "x" * (MAX_TYPE_TEXT_CHARS + 1)

    try:
        await run_burst([{"type": "type_text", "text": too_long, "method": "print"}], backend=be)
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "type_text action 0" in str(exc)

    assert be.calls == []


async def test_burst_rejects_whitespace_only_type_text_before_hid() -> None:
    be = FakeBackend()

    with pytest.raises(BurstError, match="whitespace-only"):
        await run_burst(
            [
                {
                    "type": "type_text",
                    "text": "    ",
                    "verification": "exact",
                }
            ],
            backend=be,
        )

    assert be.calls == []


async def test_burst_rejects_total_type_text_over_limit_before_hid() -> None:
    be = FakeBackend()
    chunk = "x" * min(MAX_TYPE_TEXT_CHARS, max(1, MAX_BURST_TYPE_TEXT_CHARS // 4))
    count = (MAX_BURST_TYPE_TEXT_CHARS // len(chunk)) + 1
    actions = [{"type": "type_text", "text": chunk} for _ in range(count)]

    try:
        await run_burst(actions, backend=be)
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "typed chars" in str(exc)

    assert be.calls == []


async def test_burst_rejects_chunked_base64_file_transfer_before_hid() -> None:
    be = FakeBackend()
    encoded_chunk = (
        "ZXhhY3QtcGF5bG9hZC10aGF0LW11c3Qtbm90LWJlLXR5cGVkLXRo"
        "cm91Z2gtcmF3LUhJRC1hcy1hLXRyYW5zZmVyLWhhY2s="
    )

    try:
        await run_burst(
            [
                {
                    "type": "type_text",
                    "text": f"printf '%s' '{encoded_chunk}' >> /tmp/payload.b64",
                    "context": "terminal",
                }
            ],
            backend=be,
        )
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "encoded file-transfer payload" in str(exc)

    assert be.calls == []


async def test_burst_rejects_base64_transfer_split_across_type_actions() -> None:
    be = FakeBackend()
    encoded_chunk = (
        "ZXhhY3QtcGF5bG9hZC10aGF0LW11c3Qtbm90LWJlLXR5cGVkLXRo"
        "cm91Z2gtcmF3LUhJRC1hcy1hLXRyYW5zZmVyLWhhY2s="
    )

    try:
        await run_burst(
            [
                {
                    "type": "type_text",
                    "text": "printf '%s' '",
                    "context": "terminal",
                },
                {
                    "type": "type_text",
                    "text": encoded_chunk,
                    "context": "terminal",
                },
                {
                    "type": "type_text",
                    "text": "' >> /tmp/payload.b64",
                    "context": "terminal",
                },
            ],
            backend=be,
        )
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "encoded file-transfer payload" in str(exc)

    assert be.calls == []


async def test_burst_rejects_encoded_powershell_before_hid() -> None:
    be = FakeBackend()
    encoded_command = (
        "VwByAGkAdABlAC0ATwB1AHQAcAB1AHQAIAAnAHQAaABpAHMAIABpAHMA"
        "IABhAG4AIAB1AG4AaQBuAHMAcABlAGMAdABhAGIAbABlACAAcwBjAHIAaQBwAHQAJwA="
    )

    try:
        await run_burst(
            [
                {
                    "type": "type_text",
                    "text": f"powershell -NoProfile -EncodedCommand {encoded_command}",
                    "context": "terminal",
                }
            ],
            backend=be,
        )
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "encoded shell command" in str(exc)

    assert be.calls == []


async def test_burst_rejects_heredoc_opener_before_hid() -> None:
    be = FakeBackend()

    try:
        await run_burst(
            [
                {
                    "type": "type_text",
                    "text": "python - <<'PY'",
                    "context": "terminal",
                }
            ],
            backend=be,
        )
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "heredoc shell payload" in str(exc)

    assert be.calls == []


async def test_burst_rejects_dense_nested_shell_payload_before_hid() -> None:
    be = FakeBackend()
    command = (
        "bash -lc \"python -c \\\"from pathlib import Path; "
        "p=Path('/tmp/example'); "
        "p.write_text(p.read_text().replace('old','new'))\\\" "
        "&& grep -n \\\"new\\\" /tmp/example\""
    )

    try:
        await run_burst(
            [
                {
                    "type": "type_text",
                    "text": command,
                    "context": "terminal",
                }
            ],
            backend=be,
        )
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "complex nested shell payload" in str(exc)

    assert be.calls == []


async def test_burst_allows_prose_and_short_inspectable_terminal_text() -> None:
    safe_inputs = [
        {
            "type": "type_text",
            "text": "Base64 is an encoding, not a reliable file-transfer channel.",
            "context": "editor",
        },
        {
            "type": "type_text",
            "text": "bash -lc \"pwd\"",
            "context": "terminal",
        },
        {
            "type": "type_text",
            "text": "printf '%s' 'short status' >> /tmp/status.log",
            "context": "terminal",
        },
    ]

    for action in safe_inputs:
        be = FakeBackend()
        out = await run_burst([action], backend=be)
        assert out.status == "completed"
        assert any(method == "type_text" for method, _ in be.calls)


@pytest.mark.parametrize(
    "text, reason",
    [
        ("A prose action must not end with an invisible boundary. ", "end in whitespace"),
        ("A prose action must not contain  doubled spaces.", "repeated spaces"),
    ],
)
async def test_burst_rejects_ambiguous_editor_prose_whitespace_before_hid(
    text: str,
    reason: str,
) -> None:
    backend = FakeBackend()

    with pytest.raises(BurstError, match=reason):
        await run_burst(
            [{"type": "type_text", "text": text, "context": "editor"}],
            backend=backend,
        )

    assert backend.calls == []


async def test_burst_allows_explicit_format_sensitive_repeated_spaces() -> None:
    backend = FakeBackend()

    outcome = await run_burst(
        [
            {
                "type": "type_text",
                "text": "alpha  beta",
                "context": "editor",
                "code": True,
                "verification": "exact",
            }
        ],
        backend=backend,
    )

    assert outcome.status == "completed"
    assert (
        "type_text",
        {"text": "alpha  beta", "code": True, "secret": False},
    ) in backend.calls


async def test_burst_allows_one_leading_space_for_an_editor_continuation() -> None:
    backend = FakeBackend()

    outcome = await run_burst(
        [
            {
                "type": "type_text",
                "text": " and this continuation has one explicit boundary.",
                "context": "editor",
            }
        ],
        backend=backend,
    )

    assert outcome.status == "completed"


async def test_burst_backend_failure_is_reported_not_raised() -> None:
    be = FakeBackend()

    async def boom(*_a, **_k):
        raise RuntimeError("hid offline")

    be.keypress = boom  # type: ignore[method-assign]
    out = await run_burst([{"type": "key", "keys": ["KeyA"]}], backend=be)
    assert out.status == "failed" and "hid offline" in out.error


class _StubTyper:
    """Stand-in watched typer that returns a chosen verification status."""
    def __init__(
        self,
        status: str,
        *,
        verdict: str = "match",
        field_text: str = "",
        typed_characters: int = 0,
        intended_characters: int = 0,
        correction_count: int = 0,
        delivery_retries: int = 0,
        used_fast_path: bool = False,
        emitted_characters: int | None = None,
        emitted_sha256: str = "",
        emitted_exactly_once: bool | None = None,
        readback_frame_sha256: str = "",
    ) -> None:
        self.status = status
        self.verdict = verdict
        self.field_text = field_text
        self.typed_characters = typed_characters
        self.intended_characters = intended_characters
        self.correction_count = correction_count
        self.delivery_retries = delivery_retries
        self.used_fast_path = used_fast_path
        self.emitted_characters = emitted_characters
        self.emitted_sha256 = emitted_sha256
        self.emitted_exactly_once = emitted_exactly_once
        self.readback_frame_sha256 = readback_frame_sha256
        self.calls: list[str] = []
        self.modes: list[dict[str, bool]] = []
        self.exact_modes: list[bool | None] = []

    async def type_text(
        self,
        text,
        *,
        code=False,
        prose=False,
        exact=None,
        secret=False,
        context="",
        should_continue=None,
    ):
        self.calls.append(text)
        self.exact_modes.append(exact)
        self.modes.append(
            {"code": bool(code), "prose": bool(prose), "secret": bool(secret)}
        )
        class _R:
            pass
        r = _R()
        r.status = self.status
        r.verdict = self.verdict
        r.ok = not self.status.startswith("failed_")
        r.summary = "stub"
        r.field_text = self.field_text
        r.typed_characters = self.typed_characters
        r.intended_characters = self.intended_characters
        r.correction_count = self.correction_count
        r.delivery_retries = self.delivery_retries
        r.used_fast_path = self.used_fast_path
        if self.emitted_characters is not None:
            r.emitted_characters = self.emitted_characters
        if self.emitted_sha256:
            r.emitted_sha256 = self.emitted_sha256
        if self.emitted_exactly_once is not None:
            r.emitted_exactly_once = self.emitted_exactly_once
        if self.readback_frame_sha256:
            r.readback_frame_sha256 = self.readback_frame_sha256
        return r


async def test_editor_prose_can_explicitly_use_lenient_watched_mode() -> None:
    text = (
        "Shakespeare treats choice as a human burden; his characters inherit "
        "pressure and prophecy, but they remain responsible for what follows."
    )
    be = FakeBackend()
    typer = _StubTyper("verified_safe_normalized")

    outcome = await run_burst(
        [
            {
                "type": "type_text",
                "text": text,
                "context": "editor",
                "verification": "auto",
            }
        ],
        backend=be,
        typer=typer,
    )

    assert outcome.status == "completed"
    assert typer.modes == [{"code": False, "prose": True, "secret": False}]
    assert typer.exact_modes == [False]


async def test_ordinary_direct_typing_defaults_to_exact_ocr() -> None:
    text = "quarterly earnings"
    typer = _StubTyper(
        "verified_exact",
        field_text=text,
        typed_characters=len(text),
        intended_characters=len(text),
    )

    outcome = await run_burst(
        [{"type": "type_text", "text": text}],
        backend=FakeBackend(),
        typer=typer,
    )

    assert outcome.status == "completed"
    assert typer.exact_modes == [True]
    assert outcome.action_receipts[0]["proof_state"] == "exact_ocr_readback"


async def test_editor_prose_defaults_to_exact_readback() -> None:
    text = (
        "Shakespeare treats choice as a human burden; his characters inherit "
        "pressure and prophecy, but they remain responsible for what follows."
    )
    be = FakeBackend()
    typer = _StubTyper(
        "verified_exact",
        field_text=text,
        typed_characters=len(text),
        intended_characters=len(text),
    )

    outcome = await run_burst(
        [{"type": "type_text", "text": text, "context": "editor"}],
        backend=be,
        typer=typer,
    )

    assert outcome.status == "completed"
    assert typer.modes == [{"code": False, "prose": True, "secret": False}]
    assert typer.exact_modes == [True]
    assert outcome.action_receipts[0]["proof_state"] == "exact_ocr_readback"


async def test_editor_prose_can_require_exact_ocr_checksum_proof() -> None:
    text = (
        "Shakespeare treats choice as a human burden, and every word here "
        "must retain exactly one space."
    )
    be = FakeBackend()
    typer = _StubTyper(
        "verified_exact",
        field_text=text,
        typed_characters=len(text),
        intended_characters=len(text),
    )

    outcome = await run_burst(
        [
            {
                "type": "type_text",
                "text": text,
                "context": "editor",
                "verification": "exact",
            }
        ],
        backend=be,
        typer=typer,
    )

    assert outcome.status == "completed"
    assert typer.modes == [{"code": False, "prose": True, "secret": False}]
    assert typer.exact_modes == [True]
    assert (
        outcome.action_receipts[0]["exact_readback_sha256_match"]
        is True
    )
    assert outcome.action_receipts[0]["proof_state"] == "exact_ocr_readback"


async def test_exact_receipt_binds_delivery_emission_ocr_and_screen_frame() -> None:
    text = "exactly one space"
    digest = hashlib.sha256(text.encode()).hexdigest()
    frame_digest = hashlib.sha256(b"captured screen pixels").hexdigest()
    typer = _StubTyper(
        "verified_exact",
        field_text=text,
        typed_characters=len(text),
        intended_characters=len(text),
        emitted_characters=len(text),
        emitted_sha256=digest,
        emitted_exactly_once=True,
        readback_frame_sha256=frame_digest,
    )

    outcome = await run_burst(
        [
            {
                "type": "type_text",
                "text": text,
                "context": "editor",
                "verification": "exact",
            }
        ],
        backend=FakeBackend(),
        typer=typer,
    )

    receipt = outcome.action_receipts[0]
    assert receipt["delivery_sha256"] == digest
    assert receipt["emitted_sha256"] == digest
    assert receipt["readback_sha256"] == digest
    assert receipt["readback_frame_sha256"] == frame_digest
    assert receipt["emitted_characters"] == len(text)
    assert receipt["emitted_exactly_once"] is True
    assert receipt["exact_readback_sha256_match"] is True
    assert receipt["proof_state"] == "exact_visual_readback"


async def test_exact_status_with_different_readback_bytes_stops_before_enter() -> None:
    text = "1. Observe"
    typer = _StubTyper(
        "verified_exact",
        field_text="1.\nObserve",
        typed_characters=len(text),
        intended_characters=len(text),
        emitted_characters=len(text),
        emitted_sha256=hashlib.sha256(text.encode()).hexdigest(),
        emitted_exactly_once=True,
        readback_frame_sha256=hashlib.sha256(b"screen").hexdigest(),
    )
    backend = FakeBackend()

    outcome = await run_burst(
        [
            {
                "type": "type_text",
                "text": text,
                "context": "editor",
                "verification": "exact",
            },
            {"type": "key", "keys": ["ENTER"]},
        ],
        backend=backend,
        typer=typer,
    )

    assert outcome.status == "failed"
    assert outcome.reason == "type_unverified"
    receipt = outcome.action_receipts[0]
    assert receipt["status"] == "unverified_exact_hash_mismatch"
    assert receipt["exact_readback_sha256_match"] is False
    assert receipt["proof_state"] == "ambiguous_readback"
    assert not any(method == "keypress" for method, _ in backend.calls)


async def test_exact_receipt_hashes_the_canonical_delivery_payload() -> None:
    requested = "exactly one \n space"
    delivered = "exactly one space"
    be = FakeBackend()
    typer = _StubTyper(
        "verified_exact",
        field_text=delivered,
        typed_characters=len(delivered),
        intended_characters=len(delivered),
    )

    outcome = await run_burst(
        [
            {
                "type": "type_text",
                "text": requested,
                "context": "editor",
                "verification": "exact",
            }
        ],
        backend=be,
        typer=typer,
    )

    receipt = outcome.action_receipts[0]
    assert receipt["requested_sha256"] == hashlib.sha256(
        requested.encode()
    ).hexdigest()
    assert receipt["delivery_sha256"] == hashlib.sha256(
        delivered.encode()
    ).hexdigest()
    assert receipt["issued_prefix_sha256"] == receipt["delivery_sha256"]
    assert receipt["readback_sha256"] == receipt["delivery_sha256"]
    assert receipt["delivery_transformed"] is True
    assert receipt["delivery_characters"] == len(delivered)
    assert receipt["exact_readback_sha256_match"] is True
    assert receipt["proof_state"] == "exact_ocr_readback"


async def test_burst_rejects_unknown_text_verification_mode_before_hid() -> None:
    backend = FakeBackend()

    with pytest.raises(BurstError, match="verification"):
        await run_burst(
            [
                {
                    "type": "type_text",
                    "text": "payload",
                    "verification": "trust-me",
                }
            ],
            backend=backend,
        )

    assert backend.calls == []


async def test_editor_label_cannot_relax_command_or_code_text() -> None:
    be = FakeBackend()
    command = (
        "terraform apply -auto-approve; rm -rf ./state "
        "because this remains exact command text"
    )
    typer = _StubTyper(
        "verified_exact",
        field_text=command,
        typed_characters=len(command),
        intended_characters=len(command),
    )

    outcome = await run_burst(
        [{"type": "type_text", "text": command, "context": "editor"}],
        backend=be,
        typer=typer,
    )

    assert outcome.status == "completed"
    assert typer.modes == [{"code": True, "prose": False, "secret": False}]


async def test_burst_type_text_verifies_and_stops_on_mismatch() -> None:
    # Confirmed-wrong typing must stop the burst BEFORE the following Enter (the Ctrl+F risk).
    be = FakeBackend()
    typer = _StubTyper("failed_focus_lost")
    out = await run_burst(
        [{"type": "type_text", "text": "securityadmin"}, {"type": "key", "keys": ["ENTER"]}],
        backend=be, typer=typer)
    assert out.status == "failed" and out.reason == "type_unverified"
    assert typer.calls == ["securityadmin"]
    assert not any(m == "keypress" for m, _ in be.calls)  # ENTER never ran


async def test_burst_type_text_proceeds_when_verified() -> None:
    be = FakeBackend()
    typer = _StubTyper(
        "verified_exact",
        field_text="hi",
        typed_characters=2,
        intended_characters=2,
        correction_count=1,
        delivery_retries=1,
    )
    out = await run_burst(
        [{"type": "type_text", "text": "hi"}, {"type": "key", "keys": ["ENTER"]}],
        backend=be, typer=typer)
    assert out.status == "completed"
    assert any(m == "keypress" for m, _ in be.calls)
    assert out.action_receipts == [
        {
            "index": 0,
            "type": "type_text",
            "status": "verified_exact",
            "verdict": "match",
            "observed_text": "hi",
            "observed_text_redacted": False,
                "issued_characters": 2,
                "requested_characters": 2,
                "delivery_characters": 2,
                "delivery_transformed": False,
                "observed_characters": 2,
            "correction_count": 1,
            "delivery_retries": 1,
            "used_fast_path": False,
            "summary": "stub",
            "edit_distance": 0,
            "focus_evidence": "read_back_verified",
            "proof_state": "exact_ocr_readback",
                "requested_sha256": hashlib.sha256(b"hi").hexdigest(),
                "delivery_sha256": hashlib.sha256(b"hi").hexdigest(),
                "issued_prefix_sha256": hashlib.sha256(b"hi").hexdigest(),
            "readback_sha256": hashlib.sha256(b"hi").hexdigest(),
            "exact_readback_sha256_match": True,
        }
    ]


async def test_burst_retains_watched_readback_when_typing_fails() -> None:
    be = FakeBackend()
    typer = _StubTyper(
        "failed_focus_lost",
        verdict="mismatch",
        field_text="wrong",
        typed_characters=5,
        intended_characters=8,
    )

    out = await run_burst(
        [{"type": "type_text", "text": "intended"}],
        backend=be,
        typer=typer,
    )

    assert out.status == "failed"
    assert out.action_receipts == [
        {
            "index": 0,
            "type": "type_text",
            "status": "failed_focus_lost",
            "verdict": "mismatch",
            "observed_text": "wrong",
            "observed_text_redacted": False,
                "issued_characters": 5,
                "requested_characters": 8,
                "delivery_characters": 8,
                "delivery_transformed": False,
                "observed_characters": 5,
            "correction_count": 0,
            "delivery_retries": 0,
            "used_fast_path": False,
            "summary": "stub",
            "edit_distance": 7,
            "focus_evidence": "focus_lost",
            "proof_state": "mismatched_readback",
                "requested_sha256": hashlib.sha256(b"intended").hexdigest(),
                "delivery_sha256": hashlib.sha256(b"intended").hexdigest(),
                "issued_prefix_sha256": hashlib.sha256(b"inten").hexdigest(),
            "readback_sha256": hashlib.sha256(b"wrong").hexdigest(),
            "exact_readback_sha256_match": False,
        }
    ]


async def test_burst_secret_receipt_never_retains_secret_text() -> None:
    be = FakeBackend()

    out = await run_burst(
        [{"type": "type_text", "text": "super-secret", "secret": True}],
        backend=be,
        typer=_StubTyper("verified_exact", field_text="super-secret"),
    )

    assert out.status == "completed"
    assert out.action_receipts == [
        {
            "index": 0,
            "type": "type_text",
            "status": "delivered_unverified",
            "verdict": "unverified",
            "observed_text_redacted": True,
                "issued_characters": 12,
                "requested_characters": 12,
                "delivery_characters": 12,
                "delivery_transformed": False,
                "correction_count": 0,
            "delivery_retries": 0,
            "used_fast_path": False,
            "focus_evidence": "read_back_not_retained",
            "proof_state": "not_retained",
        }
    ]
    assert "super-secret" not in repr(out.action_receipts)
    assert "sha256" not in repr(out.action_receipts)


async def test_burst_receipt_hashes_preserve_repeated_space_differences() -> None:
    backend = FakeBackend()
    typer = _StubTyper(
        "verified_safe_normalized",
        field_text="one  space",
        typed_characters=9,
        intended_characters=9,
    )

    outcome = await run_burst(
        [{"type": "type_text", "text": "one space"}],
        backend=backend,
        typer=typer,
    )

    receipt = outcome.action_receipts[0]
    assert receipt["requested_sha256"] != receipt["readback_sha256"]
    assert receipt["exact_readback_sha256_match"] is False


async def test_burst_precise_text_stops_on_ambiguous_ocr_before_enter() -> None:
    be = FakeBackend()
    typer = _StubTyper("unverified_ambiguous")
    out = await run_burst(
        [
            {"type": "type_text", "text": "rm build"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        backend=be,
        typer=typer,
    )

    assert out.status == "failed"
    assert out.reason == "type_unverified"
    assert not any(m == "keypress" for m, _ in be.calls)


async def test_burst_long_prose_stops_on_ambiguous_ocr_before_enter() -> None:
    be = FakeBackend()
    typer = _StubTyper("unverified_ambiguous")
    out = await run_burst(
        [
            {
                "type": "type_text",
                "text": "A long prose draft that still needs exact readback.",
            },
            {"type": "key", "keys": ["ENTER"]},
        ],
        backend=be,
        typer=typer,
    )

    assert out.status == "failed"
    assert out.reason == "type_unverified"
    assert not any(m == "keypress" for m, _ in be.calls)


async def test_burst_ambiguous_text_allows_only_passive_evidence_waits() -> None:
    be = FakeBackend()
    typer = _StubTyper("unverified_ambiguous")

    out = await run_burst(
        [
            {
                "type": "type_text",
                "text": "ls -l",
                "context": "terminal",
            },
            {"type": "wait", "ms": 0},
        ],
        backend=be,
        typer=typer,
    )

    assert out.status == "unverified"
    assert out.completed == out.total == 2
    assert out.reason == "type_unverified"
    assert not any(method == "keypress" for method, _ in be.calls)


async def test_field_text_is_verified_exactly_before_followup_action() -> None:
    be = FakeBackend()
    typer = _StubTyper("unverified_ambiguous")
    out = await run_burst(
        [
            {
                "type": "type_text",
                "text": "Dim screen when inactive",
                "context": "field",
            },
            {"type": "key", "keys": ["ENTER"]},
        ],
        backend=be,
        typer=typer,
    )

    assert out.status == "failed"
    assert out.reason == "type_unverified"
    assert not any(m == "keypress" for m, _ in be.calls)


async def test_burst_reports_partial_type_progress_when_deadline_interrupts() -> None:
    be = FakeBackend()
    typer = _StubTyper(
        "blocked_by_policy",
        typed_characters=16,
        intended_characters=191,
    )

    out = await run_burst(
        [
            {"type": "type_text", "text": "x" * 191, "code": True},
            {"type": "key", "keys": ["ENTER"]},
        ],
        backend=be,
        typer=typer,
    )

    assert out.status == "interrupted"
    assert out.completed == 0
    assert out.partial_action == {
        "type": "type_text",
        "issued_characters": 16,
        "requested_characters": 191,
    }
    assert not any(method == "keypress" for method, _ in be.calls)


async def test_burst_print_method_cannot_bypass_watched_verification() -> None:
    be = FakeBackend()
    typer = _StubTyper("unverified_ambiguous")
    out = await run_burst(
        [
            {"type": "type_text", "text": "long", "method": "print"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        backend=be,
        typer=typer,
    )

    assert out.status == "failed"
    assert out.reason == "type_unverified"
    assert typer.calls == ["long"]
    assert not any(method == "print_text" for method, _ in be.calls)
    assert not any(method == "keypress" for method, _ in be.calls)


async def test_burst_defers_exact_editor_rows_to_one_post_burst_verifier() -> None:
    be = FakeBackend()
    typer = _StubTyper("verified_exact")
    rows = ("@echo off", "  exit /b 0")

    out = await run_burst(
        [
            {
                "type": "type_text",
                "text": rows[0],
                "code": True,
                "context": "editor",
                "verification": "deferred_exact",
            },
            {"type": "key", "keys": ["SHIFT", "ENTER"]},
            {
                "type": "type_text",
                "text": rows[1],
                "code": True,
                "context": "editor",
                "verification": "deferred_exact",
            },
        ],
        backend=be,
        typer=typer,
    )

    assert out.status == "completed"
    assert typer.calls == []
    assert [
        call["text"] for method, call in be.calls if method == "print_text"
    ] == list(rows)
    assert [
        call["keys"] for method, call in be.calls if method == "keypress"
    ] == [["ShiftLeft", "Enter"]]
    assert [receipt["status"] for receipt in out.action_receipts] == [
        "delivered_unverified",
        "delivered_unverified",
    ]
    assert all(
        receipt["emitted_exactly_once"] is True
        and receipt["focus_evidence"] == "read_back_deferred"
        for receipt in out.action_receipts
    )


@pytest.mark.parametrize(
    "actions",
    [
        [
            {
                "type": "type_text",
                "text": "unsafe single row",
                "code": True,
                "context": "editor",
                "verification": "deferred_exact",
            }
        ],
        [
            {
                "type": "type_text",
                "text": "row one",
                "code": True,
                "context": "field",
                "verification": "deferred_exact",
            },
            {"type": "key", "keys": ["SHIFT", "ENTER"]},
            {
                "type": "type_text",
                "text": "row two",
                "code": True,
                "context": "field",
                "verification": "deferred_exact",
            },
        ],
        [
            {
                "type": "type_text",
                "text": "row one",
                "code": True,
                "context": "editor",
                "verification": "deferred_exact",
            },
            {"type": "key", "keys": ["ENTER"]},
            {
                "type": "type_text",
                "text": "row two",
                "code": True,
                "context": "editor",
                "verification": "deferred_exact",
            },
        ],
    ],
)
async def test_burst_rejects_deferred_readback_outside_inert_multiline_editor(
    actions,
) -> None:
    be = FakeBackend()

    with pytest.raises(BurstError, match="deferred_exact"):
        await run_burst(actions, backend=be)

    assert be.calls == []


async def test_burst_rejects_no_verify_escape_hatch_before_hid() -> None:
    be = FakeBackend()

    try:
        await run_burst(
            [{"type": "type_text", "text": "payload", "no_verify": True}],
            backend=be,
        )
        assert False, "expected BurstError"
    except BurstError as exc:
        assert "no_verify" in str(exc)

    assert be.calls == []


async def test_burst_print_method_stops_between_bounded_chunks() -> None:
    """Regression: a server-side print must not keep draining after panic-stop."""
    be = FakeBackend()

    def gate() -> bool:
        return sum(method == "print_text" for method, _ in be.calls) < 1

    out = await run_burst(
        [{"type": "type_text", "text": "x" * 64, "method": "print"}],
        backend=be,
        should_continue=gate,
    )

    printed = [call["text"] for method, call in be.calls if method == "print_text"]
    assert printed == ["x" * 16]
    assert out.status == "interrupted"
    assert out.reason == "control_changed"
    assert any(method == "release_all" for method, _ in be.calls)
