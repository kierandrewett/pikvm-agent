"""Bounded, redacted input-delivery evidence for public harness events."""

from __future__ import annotations

import re
from typing import Any

from pikvm_agent.pikvm.text import flatten_line_breaks


def public_input_receipts(
    raw: dict[str, Any],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy watched-typing evidence across the public event boundary.

    The daemon result is tool-controlled and may include exact field contents.
    Only known receipt fields are retained, secret inputs are forced back to an
    unverified delivery-only state, and every receipt must correspond to an
    actual text or bounded spreadsheet action in the attempted input sequence.
    """

    candidates = raw.get("action_receipts")
    if not isinstance(candidates, list):
        return []
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    allowed_strings = {
        "status": {
            "verified_exact",
            "verified_safe_normalized",
            "verified_with_warnings",
            "unverified_ambiguous",
            "unverified_whitespace",
            "unverified_wrong_region",
            "unverified_truncated",
            "failed_symbol_mismatch",
            "failed_case_mismatch",
            "failed_keyboard_layout",
            "failed_focus_lost",
            "failed_stale_frame",
            "blocked_by_policy",
            "needs_human",
            "delivered_unverified",
        },
        "verdict": {"match", "contains", "mismatch", "unverified"},
        "focus_evidence": {
            "atomic_windows_run_gesture",
            "focus_lost",
            "read_back_deferred",
            "read_back_verified",
            "read_back_unverified",
            "read_back_mismatch",
            "read_back_not_retained",
            "read_back_unavailable",
        },
        "proof_state": {
            "exact_ocr_readback",
            "exact_visual_readback",
            # Accepted for old daemon receipts; output is normalized below.
            "exact_readback",
            "normalized_readback",
            "partial_readback",
            "mismatched_readback",
            "ambiguous_readback",
            "issued_only",
            "not_retained",
        },
    }
    integer_limits = {
        "requested_cells": 64,
        "issued_cells": 64,
        "requested_characters": 480,
        "delivery_characters": 480,
        "issued_characters": 480,
        "observed_characters": 960,
        "correction_count": 20,
        "delivery_retries": 20,
        "emitted_characters": 1_920,
        "edit_distance": 960,
    }
    for candidate in candidates[:20]:
        if not isinstance(candidate, dict):
            continue
        index = candidate.get("index")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= len(actions)
            or index in seen
        ):
            continue
        action = actions[index]
        action_type = action.get("type")
        if action_type not in {"type_text", "spreadsheet_grid"}:
            continue
        seen.add(index)
        secret = action_type == "type_text" and action.get("secret") is True
        redacted = secret or candidate.get("observed_text_redacted") is True
        receipt: dict[str, Any] = {
            "index": index,
            "type": action_type,
        }
        for key, allowed in allowed_strings.items():
            value = candidate.get(key)
            if isinstance(value, str) and value in allowed:
                receipt[key] = value
        for key, limit in integer_limits.items():
            legacy_key = {
                "requested_characters": "intended_characters",
                "issued_characters": "typed_characters",
            }.get(key)
            value = candidate.get(key)
            if value is None and legacy_key is not None:
                value = candidate.get(legacy_key)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= limit
            ):
                receipt[key] = value
        used_fast_path = candidate.get("used_fast_path")
        if isinstance(used_fast_path, bool):
            receipt["used_fast_path"] = used_fast_path
        delivery_transformed = candidate.get("delivery_transformed")
        if isinstance(delivery_transformed, bool):
            receipt["delivery_transformed"] = delivery_transformed
        for key, legacy_key in (
            ("requested_sha256", "intended_sha256"),
            ("delivery_sha256", ""),
            ("issued_prefix_sha256", "acknowledged_prefix_sha256"),
            ("emitted_sha256", ""),
            ("readback_sha256", "observed_sha256"),
            ("readback_frame_sha256", ""),
        ):
            value = candidate.get(key)
            if value is None and legacy_key:
                value = candidate.get(legacy_key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                receipt[key] = value
        exact_sha256_match = candidate.get("exact_readback_sha256_match")
        if exact_sha256_match is None:
            exact_sha256_match = candidate.get("exact_sha256_match")
        if isinstance(exact_sha256_match, bool):
            receipt["exact_readback_sha256_match"] = exact_sha256_match
        emitted_exactly_once = candidate.get("emitted_exactly_once")
        if isinstance(emitted_exactly_once, bool):
            receipt["emitted_exactly_once"] = emitted_exactly_once
        receipt["observed_text_redacted"] = redacted
        if redacted:
            for key in (
                "requested_sha256",
                "delivery_sha256",
                "issued_prefix_sha256",
                "emitted_sha256",
                "readback_sha256",
                "readback_frame_sha256",
                "exact_readback_sha256_match",
                "emitted_exactly_once",
            ):
                receipt.pop(key, None)
            receipt.update(
                {
                    "status": "delivered_unverified",
                    "verdict": "unverified",
                    "focus_evidence": "read_back_not_retained",
                    "proof_state": "not_retained",
                }
            )
        elif action_type == "type_text":
            observed_text = candidate.get("observed_text")
            if isinstance(observed_text, str) and len(observed_text) <= 960:
                receipt["observed_text"] = observed_text
                receipt["observed_characters"] = len(observed_text)
            summary = candidate.get("summary")
            if isinstance(summary, str) and 0 < len(summary) <= 320:
                receipt["summary"] = summary
            receipt["proof_state"] = _proof_state(
                receipt,
                observed_text=(
                    observed_text
                    if isinstance(observed_text, str)
                    else ""
                ),
                intended_text=flatten_line_breaks(
                    str(action.get("text") or "")
                ),
            )
        else:
            receipt["proof_state"] = "issued_only"
        output.append(receipt)
    return output


def _proof_state(
    receipt: dict[str, Any],
    *,
    observed_text: str,
    intended_text: str,
) -> str:
    """Name exactly what the receipt proves; sender completion is not target ACK."""

    if (
        receipt.get("status") == "verified_exact"
        and receipt.get("verdict") == "match"
        and receipt.get("focus_evidence") == "read_back_verified"
        and receipt.get("exact_readback_sha256_match") is True
        and receipt.get("issued_characters")
        == receipt.get(
            "delivery_characters",
            receipt.get("requested_characters"),
        )
        and receipt.get(
            "delivery_sha256",
            receipt.get("requested_sha256"),
        )
        == receipt.get("issued_prefix_sha256")
        and receipt.get(
            "delivery_sha256",
            receipt.get("requested_sha256"),
        )
        == receipt.get("readback_sha256")
    ):
        if re.fullmatch(
            r"[0-9a-f]{64}",
            str(receipt.get("readback_frame_sha256") or ""),
        ):
            return "exact_visual_readback"
        return "exact_ocr_readback"
    if (
        receipt.get("status") == "verified_safe_normalized"
        and receipt.get("verdict") in {"match", "contains"}
    ):
        return "normalized_readback"
    if observed_text:
        if (
            receipt.get("status") == "unverified_truncated"
            or (
                intended_text
                and len(observed_text) < len(intended_text)
                and intended_text.startswith(observed_text)
            )
        ):
            return "partial_readback"
        if (
            receipt.get("verdict") == "mismatch"
            or str(receipt.get("status") or "").startswith("failed_")
        ):
            return "mismatched_readback"
        return "ambiguous_readback"
    return "issued_only"
