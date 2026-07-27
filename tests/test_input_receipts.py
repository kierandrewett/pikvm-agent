from __future__ import annotations

import hashlib

from pikvm_agent.harness.input_receipts import public_input_receipts


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_sender_completion_is_not_reported_as_target_acknowledgement() -> None:
    intended = "Get-Process observer*,pikvm-accuracy-observer -ea 0"
    visible_prefix = "Get-Process observer*"
    raw = {
        "action_receipts": [
            {
                "index": 0,
                "type": "type_text",
                "status": "unverified_truncated",
                "verdict": "unverified",
                "focus_evidence": "read_back_unverified",
                "typed_characters": len(intended),
                "intended_characters": len(intended),
                "intended_sha256": _sha256(intended),
                "acknowledged_prefix_sha256": _sha256(intended),
                "observed_text": visible_prefix,
                "observed_text_redacted": False,
                "observed_sha256": _sha256(visible_prefix),
                "exact_sha256_match": False,
            }
        ]
    }

    receipt = public_input_receipts(
        raw,
        [{"type": "type_text", "text": intended}],
    )[0]

    assert receipt["requested_characters"] == len(intended)
    assert receipt["issued_characters"] == len(intended)
    assert receipt["observed_characters"] == len(visible_prefix)
    assert receipt["requested_sha256"] == _sha256(intended)
    assert receipt["issued_prefix_sha256"] == _sha256(intended)
    assert receipt["readback_sha256"] == _sha256(visible_prefix)
    assert receipt["exact_readback_sha256_match"] is False
    assert receipt["proof_state"] == "partial_readback"
    assert "typed_characters" not in receipt
    assert "acknowledged_prefix_sha256" not in receipt


def test_exact_ocr_readback_is_the_only_exact_target_text_proof() -> None:
    intended = "one space"
    raw = {
        "action_receipts": [
            {
                "index": 0,
                "type": "type_text",
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "typed_characters": len(intended),
                "intended_characters": len(intended),
                "intended_sha256": _sha256(intended),
                "acknowledged_prefix_sha256": _sha256(intended),
                "observed_text": intended,
                "observed_text_redacted": False,
                "observed_sha256": _sha256(intended),
                "exact_sha256_match": True,
            }
        ]
    }

    receipt = public_input_receipts(
        raw,
        [{"type": "type_text", "text": intended}],
    )[0]

    assert receipt["proof_state"] == "exact_ocr_readback"
    assert receipt["exact_readback_sha256_match"] is True


def test_visual_readback_preserves_the_full_checksum_chain() -> None:
    intended = "one space"
    intended_sha256 = _sha256(intended)
    frame_sha256 = _sha256("captured pixels")
    raw = {
        "action_receipts": [
            {
                "index": 0,
                "type": "type_text",
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "requested_characters": len(intended),
                "delivery_characters": len(intended),
                "issued_characters": len(intended),
                "emitted_characters": len(intended),
                "requested_sha256": intended_sha256,
                "delivery_sha256": intended_sha256,
                "issued_prefix_sha256": intended_sha256,
                "emitted_sha256": intended_sha256,
                "emitted_exactly_once": True,
                "observed_text": intended,
                "observed_text_redacted": False,
                "readback_sha256": intended_sha256,
                "readback_frame_sha256": frame_sha256,
                "exact_readback_sha256_match": True,
            }
        ]
    }

    receipt = public_input_receipts(
        raw,
        [{"type": "type_text", "text": intended}],
    )[0]

    assert receipt["proof_state"] == "exact_visual_readback"
    assert receipt["emitted_sha256"] == intended_sha256
    assert receipt["readback_frame_sha256"] == frame_sha256
    assert receipt["emitted_exactly_once"] is True


def test_public_receipt_keeps_requested_and_delivery_hashes_distinct() -> None:
    requested = "one \n space"
    delivered = "one space"
    raw = {
        "action_receipts": [
            {
                "index": 0,
                "type": "type_text",
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "requested_characters": len(requested),
                "delivery_characters": len(delivered),
                "issued_characters": len(delivered),
                "delivery_transformed": True,
                "requested_sha256": _sha256(requested),
                "delivery_sha256": _sha256(delivered),
                "issued_prefix_sha256": _sha256(delivered),
                "observed_text": delivered,
                "observed_text_redacted": False,
                "readback_sha256": _sha256(delivered),
                "exact_readback_sha256_match": True,
            }
        ]
    }

    receipt = public_input_receipts(
        raw,
        [{"type": "type_text", "text": requested}],
    )[0]

    assert receipt["requested_sha256"] == _sha256(requested)
    assert receipt["delivery_sha256"] == _sha256(delivered)
    assert receipt["delivery_transformed"] is True
    assert receipt["proof_state"] == "exact_ocr_readback"


def test_exact_status_cannot_hide_an_incomplete_sender_prefix() -> None:
    intended = "one space"
    raw = {
        "action_receipts": [
            {
                "index": 0,
                "type": "type_text",
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "issued_characters": 3,
                "requested_characters": len(intended),
                "requested_sha256": _sha256(intended),
                "issued_prefix_sha256": _sha256("one"),
                "observed_text": intended,
                "observed_text_redacted": False,
                "readback_sha256": _sha256(intended),
                "exact_readback_sha256_match": True,
            }
        ]
    }

    receipt = public_input_receipts(
        raw,
        [{"type": "type_text", "text": intended}],
    )[0]

    assert receipt["proof_state"] == "ambiguous_readback"
