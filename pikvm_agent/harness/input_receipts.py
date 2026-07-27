"""Bounded, redacted input-delivery evidence for public harness events."""

from __future__ import annotations

import re
from typing import Any


def public_input_receipts(
    raw: dict[str, Any],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy watched-typing evidence across the public event boundary.

    The daemon result is tool-controlled and may include exact field contents.
    Only known receipt fields are retained, secret inputs are forced back to an
    unverified delivery-only state, and every receipt must correspond to an
    actual type action in the attempted input sequence.
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
            "focus_lost",
            "read_back_verified",
            "read_back_unverified",
            "read_back_mismatch",
            "read_back_not_retained",
            "read_back_unavailable",
        },
    }
    integer_limits = {
        "typed_characters": 480,
        "intended_characters": 480,
        "correction_count": 20,
        "delivery_retries": 20,
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
        if action.get("type") != "type_text":
            continue
        seen.add(index)
        secret = action.get("secret") is True
        redacted = secret or candidate.get("observed_text_redacted") is True
        receipt: dict[str, Any] = {
            "index": index,
            "type": "type_text",
        }
        for key, allowed in allowed_strings.items():
            value = candidate.get(key)
            if isinstance(value, str) and value in allowed:
                receipt[key] = value
        for key, limit in integer_limits.items():
            value = candidate.get(key)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= limit
            ):
                receipt[key] = value
        used_fast_path = candidate.get("used_fast_path")
        if isinstance(used_fast_path, bool):
            receipt["used_fast_path"] = used_fast_path
        for key in (
            "intended_sha256",
            "acknowledged_prefix_sha256",
            "observed_sha256",
        ):
            value = candidate.get(key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                receipt[key] = value
        exact_sha256_match = candidate.get("exact_sha256_match")
        if isinstance(exact_sha256_match, bool):
            receipt["exact_sha256_match"] = exact_sha256_match
        receipt["observed_text_redacted"] = redacted
        if redacted:
            for key in (
                "intended_sha256",
                "acknowledged_prefix_sha256",
                "observed_sha256",
                "exact_sha256_match",
            ):
                receipt.pop(key, None)
            receipt.update(
                {
                    "status": "delivered_unverified",
                    "verdict": "unverified",
                    "focus_evidence": "read_back_not_retained",
                }
            )
        else:
            observed_text = candidate.get("observed_text")
            if isinstance(observed_text, str) and len(observed_text) <= 960:
                receipt["observed_text"] = observed_text
            summary = candidate.get("summary")
            if isinstance(summary, str) and 0 < len(summary) <= 320:
                receipt["summary"] = summary
        output.append(receipt)
    return output
