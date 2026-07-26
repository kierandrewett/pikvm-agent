"""Deterministic public scorecards backed by checked benchmark reports.

The narrative benchmark document is intentionally human-written.  Its headline
table is different: every measured value and every evidence digest is rendered
from a small manifest plus the immutable JSON reports it names.  ``--check``
therefore turns a stale public scorecard into a test failure instead of a
quietly misleading product claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCORECARD_START = "<!-- pikvm-scorecard:start -->"
SCORECARD_END = "<!-- pikvm-scorecard:end -->"
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ScorecardField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    format: Literal[
        "text",
        "integer",
        "number",
        "percent",
        "milliseconds",
        "seconds_from_ms",
        "count",
    ] = "text"
    digits: int = Field(default=2, ge=0, le=6)


class ScorecardRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: str
    route: str
    source: str
    fields: dict[str, ScorecardField]
    cases: str
    result: str
    latency: str
    wall: str
    status: str

    @model_validator(mode="after")
    def validate_fields_and_templates(self) -> "ScorecardRow":
        invalid = sorted(name for name in self.fields if not _FIELD_NAME.fullmatch(name))
        if invalid:
            raise ValueError("invalid scorecard field names: " + ", ".join(invalid))
        formatter = _StrictFormatMap({name: "value" for name in self.fields})
        for label, template in (
            ("cases", self.cases),
            ("result", self.result),
            ("latency", self.latency),
            ("wall", self.wall),
            ("status", self.status),
        ):
            try:
                template.format_map(formatter)
            except KeyError as exc:
                raise ValueError(
                    f"{label} template references unknown field {exc.args[0]!r}"
                ) from exc
        return self


class ScorecardManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    as_of: str
    rows: list[ScorecardRow] = Field(min_length=1)


class _StrictFormatMap(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def _value_at(document: Any, path: str) -> Any:
    value = document
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
            continue
        if isinstance(value, list):
            try:
                value = value[int(part)]
                continue
            except (IndexError, ValueError):
                pass
        raise ValueError(f"benchmark report has no scalar path {path!r}")
    return value


def _number(value: Any, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"benchmark field {path!r} must be numeric")
    return float(value)


def _format_field(value: Any, spec: ScorecardField) -> str:
    if spec.format == "count":
        if not isinstance(value, (dict, list)):
            raise ValueError(f"benchmark field {spec.path!r} must be a list or mapping")
        return f"{len(value):,}"
    if spec.format == "text":
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"benchmark field {spec.path!r} must be scalar")
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)
    number = _number(value, path=spec.path)
    if spec.format == "integer":
        if not number.is_integer():
            raise ValueError(f"benchmark field {spec.path!r} is not an integer")
        return f"{int(number):,}"
    if spec.format == "percent":
        return f"{number * 100:.{spec.digits}f}%"
    if spec.format == "milliseconds":
        return f"{number:,.{spec.digits}f}ms"
    if spec.format == "seconds_from_ms":
        return f"{number / 1000:,.{spec.digits}f}s"
    return f"{number:,.{spec.digits}f}"


def _safe_report_path(manifest_path: Path, source: str) -> Path:
    relative = Path(source)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("scorecard report paths must stay beneath the manifest directory")
    root = manifest_path.resolve().parent
    report_path = (root / relative).resolve()
    if not report_path.is_relative_to(root):
        raise ValueError("scorecard report path escapes the manifest directory")
    if not report_path.is_file():
        raise ValueError(f"scorecard report does not exist: {source}")
    return report_path


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_scorecard(manifest_path: Path) -> str:
    raw = yaml.safe_load(manifest_path.read_text())
    manifest = ScorecardManifest.model_validate(raw)
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()[:12]
    lines = [
        SCORECARD_START,
        (
            "_Generated from checked JSON evidence as of "
            f"{_markdown_cell(manifest.as_of)}. Manifest "
            f"`sha256:{manifest_digest}`; run "
            "`pikvm-agent harness scorecard --check` to detect drift._"
        ),
        "",
        "| Suite | Route | Cases | Result | Median / p95 | Wall | Status | Evidence |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in manifest.rows:
        report_path = _safe_report_path(manifest_path, row.source)
        report_bytes = report_path.read_bytes()
        try:
            document = json.loads(report_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid scorecard JSON report {row.source}: {exc}") from exc
        formatted = {
            name: _format_field(_value_at(document, spec.path), spec)
            for name, spec in row.fields.items()
        }
        cells = [
            row.suite,
            row.route,
            row.cases.format_map(_StrictFormatMap(formatted)),
            row.result.format_map(_StrictFormatMap(formatted)),
            row.latency.format_map(_StrictFormatMap(formatted)),
            row.wall.format_map(_StrictFormatMap(formatted)),
            row.status.format_map(_StrictFormatMap(formatted)),
        ]
        digest = hashlib.sha256(report_bytes).hexdigest()[:12]
        evidence = f"[JSON]({row.source}) · `sha256:{digest}`"
        lines.append(
            "| " + " | ".join(_markdown_cell(cell) for cell in [*cells, evidence]) + " |"
        )
    lines.append(SCORECARD_END)
    return "\n".join(lines)


def replace_scorecard(document: str, rendered: str) -> str:
    start_count = document.count(SCORECARD_START)
    end_count = document.count(SCORECARD_END)
    if start_count != 1 or end_count != 1:
        raise ValueError(
            "target document must contain exactly one scorecard start/end marker pair"
        )
    start = document.index(SCORECARD_START)
    end = document.index(SCORECARD_END, start) + len(SCORECARD_END)
    return document[:start] + rendered + document[end:]


def update_scorecard(
    *,
    manifest_path: Path,
    document_path: Path,
    check: bool,
) -> bool:
    """Return True when current; update only when ``check`` is false."""

    current = document_path.read_text()
    expected = replace_scorecard(current, render_scorecard(manifest_path))
    if current == expected:
        return True
    if check:
        return False
    document_path.write_text(expected)
    return True
