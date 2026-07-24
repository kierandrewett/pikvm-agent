"""Import prior agent transcripts as privacy-preserving regression evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_BURST_SUFFIX = "pikvm_run_burst"
_BASE64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-"
)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{80,}={0,2}")
_DANGEROUS = re.compile(
    r"(?ix)"
    r"(?:\brm\s+-[^\s]*r|\bdel(?:ete)?\b|\bremove-item\b|"
    r"\bterraform\s+apply\b|\bgit\s+push\b|\bsend\b|"
    r"\bshutdown\b|\breboot\b|\bformat\b|\bchmod\b|\bchown\b|"
    r"\binstall\b|\bpayment\b|\bpay\b)"
)


class TranscriptFinding(BaseModel):
    sequence: int
    tool_use_id: str
    kind: str
    severity: Literal["info", "warning", "critical"]
    details: dict[str, Any] = Field(default_factory=dict)


class TranscriptReport(BaseModel):
    conversation_id: str
    lines: int = 0
    malformed_lines: int = 0
    tool_counts: dict[str, int] = Field(default_factory=dict)
    bursts: int = 0
    total_typed_characters: int = 0
    maximum_text_length: int = 0
    missing_idempotency_keys: int = 0
    missing_freshness: int = 0
    unverified_print_entries: int = 0
    base64_entries: int = 0
    oversized_text_entries: int = 0
    submit_in_same_burst: int = 0
    dangerous_submissions: int = 0
    findings: list[TranscriptFinding] = Field(default_factory=list)


def analyze_claude_transcript(path: Path) -> TranscriptReport:
    report = TranscriptReport(conversation_id=path.stem)
    counts: Counter[str] = Counter()
    burst_sequence = 0
    for line in path.read_text(errors="replace").splitlines():
        report.lines += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            report.malformed_lines += 1
            continue
        for item in _content_items(row):
            if item.get("type") != "tool_use":
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            counts[name] += 1
            if not name.endswith(_BURST_SUFFIX):
                continue
            burst_sequence += 1
            report.bursts += 1
            _analyze_burst(report, burst_sequence, item)
    report.tool_counts = dict(sorted(counts.items()))
    return report


def _content_items(row: Any) -> list[dict[str, Any]]:
    if not isinstance(row, dict):
        return []
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _analyze_burst(
    report: TranscriptReport, sequence: int, item: dict[str, Any]
) -> None:
    tool_use_id = str(item.get("id") or f"burst-{sequence}")
    inputs = item.get("input") if isinstance(item.get("input"), dict) else {}
    actions = inputs.get("actions") if isinstance(inputs.get("actions"), list) else []

    if not inputs.get("idempotency_key"):
        report.missing_idempotency_keys += 1
        _finding(
            report,
            sequence,
            tool_use_id,
            "missing_idempotency",
            "warning",
            action_count=len(actions),
        )
    if (
        inputs.get("based_on_world_version") is None
        or inputs.get("based_on_control_epoch") is None
    ):
        report.missing_freshness += 1
        _finding(
            report,
            sequence,
            tool_use_id,
            "missing_freshness",
            "critical",
            action_count=len(actions),
        )

    typed: list[tuple[int, str, dict[str, Any]]] = []
    enter_indexes: list[int] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        if action.get("type") == "type_text":
            text = str(action.get("text") or "")
            typed.append((index, text, action))
            length = len(text)
            report.total_typed_characters += length
            report.maximum_text_length = max(report.maximum_text_length, length)
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            safe_details = {"length": length, "sha256_prefix": digest}
            if action.get("method") == "print":
                report.unverified_print_entries += 1
                _finding(
                    report,
                    sequence,
                    tool_use_id,
                    "unverified_print",
                    "warning",
                    **safe_details,
                )
            if _looks_base64(text):
                report.base64_entries += 1
                _finding(
                    report,
                    sequence,
                    tool_use_id,
                    "base64_payload",
                    "warning",
                    **safe_details,
                )
            if length > 120:
                report.oversized_text_entries += 1
                _finding(
                    report,
                    sequence,
                    tool_use_id,
                    "oversized_text",
                    "warning",
                    **safe_details,
                )
        if action.get("type") == "key":
            keys = action.get("keys") or []
            if any(str(key).upper() in {"ENTER", "RETURN"} for key in keys):
                enter_indexes.append(index)

    submits = [
        (index, text)
        for index, text, _ in typed
        if any(enter_index > index for enter_index in enter_indexes)
    ]
    if submits:
        report.submit_in_same_burst += 1
        _finding(
            report,
            sequence,
            tool_use_id,
            "same_burst_submit",
            "critical",
            typed_entries=len(submits),
        )
    dangerous = [text for _, text in submits if _DANGEROUS.search(text)]
    if dangerous:
        report.dangerous_submissions += 1
        _finding(
            report,
            sequence,
            tool_use_id,
            "dangerous_submission",
            "critical",
            typed_entries=len(dangerous),
        )


def _looks_base64(text: str) -> bool:
    compact = text.strip()
    if len(compact) < 80:
        return False
    if not any(char.isspace() for char in compact):
        allowed = sum(char in _BASE64_CHARS for char in compact)
        if allowed / len(compact) >= 0.97:
            return True
    candidates = _BASE64_RUN.findall(compact)
    return any(
        sum(char in _BASE64_CHARS for char in candidate) / len(candidate)
        >= 0.97
        for candidate in candidates
    )


def _finding(
    report: TranscriptReport,
    sequence: int,
    tool_use_id: str,
    kind: str,
    severity: Literal["info", "warning", "critical"],
    **details: Any,
) -> None:
    report.findings.append(
        TranscriptFinding(
            sequence=sequence,
            tool_use_id=tool_use_id,
            kind=kind,
            severity=severity,
            details=details,
        )
    )
