"""Artifact-backed acceptance contracts for autonomous Office tasks.

A model completion claim is never sufficient evidence here.  The saved OOXML
artifact is parsed on the harness host and must satisfy the declared semantic
checks before a task can pass.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import posixpath
import re
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from xml.etree import ElementTree

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.performance import (
    RunPerformanceReport,
    summarize_run_performance,
)

OFFICE_ACCEPTANCE_SCHEMA_VERSION = 1
MAX_PACKAGE_MEMBERS = 5_000
MAX_PACKAGE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_CELL_REFERENCE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_WORD = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR = "http://schemas.openxmlformats.org/package/2006/relationships"


class OfficeAcceptanceError(ValueError):
    """The artifact or acceptance contract could not be evaluated safely."""


def _safe_identifier(value: str, *, label: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{label} must be a safe stable identifier")
    return value


class OfficeCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str


class OfficeArtifactVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["docx", "xlsx"]
    passed: bool
    sha256: str
    byte_count: int = Field(ge=0)
    checks: list[OfficeCheckResult] = Field(default_factory=list)
    error: str | None = None


class DocxExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    title_style: str | None = None
    min_paragraphs: int = Field(default=1, ge=1, le=10_000)
    min_word_count: int = Field(default=1, ge=1, le=1_000_000)
    max_word_count: int | None = Field(default=None, ge=1, le=1_000_000)
    forbid_repeated_spaces: bool = False
    required_phrases: list[str] = Field(default_factory=list, max_length=200)
    exact_paragraphs: list[str] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_bounds(self) -> "DocxExpectation":
        if (
            self.max_word_count is not None
            and self.max_word_count < self.min_word_count
        ):
            raise ValueError("max_word_count cannot be below min_word_count")
        if self.title_style is not None and self.title is None:
            raise ValueError("title_style requires title")
        if any(not phrase.strip() for phrase in self.required_phrases):
            raise ValueError("required phrases must not be blank")
        return self


class CellExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None
    formula: str | None = None

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int, Decimal)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        raise ValueError("cell values must be scalar and finite")

    @field_validator("formula")
    @classmethod
    def validate_formula(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().removeprefix("=")
        if not normalized:
            raise ValueError("cell formula must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_value_or_formula(self) -> "CellExpectation":
        if "value" not in self.model_fields_set and self.formula is None:
            raise ValueError("cell expectation requires a value or formula")
        return self


class WorksheetExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cells: dict[str, CellExpectation] = Field(min_length=1, max_length=20_000)

    @field_validator("cells")
    @classmethod
    def validate_cells(
        cls,
        value: dict[str, CellExpectation],
    ) -> dict[str, CellExpectation]:
        normalized: dict[str, CellExpectation] = {}
        for reference, expected in value.items():
            cell = reference.upper()
            if not _CELL_REFERENCE.fullmatch(cell):
                raise ValueError(f"invalid cell reference: {reference}")
            if cell in normalized:
                raise ValueError(f"duplicate cell reference: {reference}")
            normalized[cell] = expected
        return normalized


class XlsxExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheets: dict[str, WorksheetExpectation] = Field(
        min_length=1,
        max_length=1_000,
    )

    @field_validator("sheets")
    @classmethod
    def validate_sheets(
        cls,
        value: dict[str, WorksheetExpectation],
    ) -> dict[str, WorksheetExpectation]:
        if any(not name.strip() for name in value):
            raise ValueError("worksheet names must not be blank")
        return value


class OfficeArtifactSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["docx", "xlsx"]
    filename: str = Field(min_length=1, max_length=96)
    max_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1,
        le=MAX_PACKAGE_UNCOMPRESSED_BYTES,
    )
    docx: DocxExpectation | None = None
    xlsx: XlsxExpectation | None = None

    @model_validator(mode="after")
    def validate_format_contract(self) -> "OfficeArtifactSpec":
        if Path(self.filename).name != self.filename:
            raise ValueError("artifact filename must be a basename")
        if not self.filename.casefold().endswith(f".{self.format}"):
            raise ValueError("artifact filename extension must match its format")
        expected = self.docx if self.format == "docx" else self.xlsx
        unexpected = self.xlsx if self.format == "docx" else self.docx
        if expected is None or unexpected is not None:
            raise ValueError(
                f"{self.format} artifact requires only its matching checks"
            )
        return self


class OfficeTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    instruction_template: str = Field(min_length=1, max_length=100_000)
    artifact: OfficeArtifactSpec

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        return _safe_identifier(value, label="task_id")

    @field_validator("instruction_template")
    @classmethod
    def validate_instruction_template(cls, value: str) -> str:
        if value.count("{artifact_path}") != 1:
            raise ValueError(
                "instruction_template must contain {artifact_path} exactly once"
            )
        unknown = re.findall(r"\{[^{}]+\}", value.replace("{artifact_path}", ""))
        if unknown:
            raise ValueError("instruction_template contains an unknown placeholder")
        return value

    def render_instruction(self, artifact_path: str) -> str:
        if not artifact_path.strip():
            raise ValueError("artifact_path must not be blank")
        return self.instruction_template.replace(
            "{artifact_path}",
            artifact_path,
        )


class OfficeAcceptanceSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    suite_id: str
    tasks: list[OfficeTaskSpec] = Field(min_length=1, max_length=10_000)

    @field_validator("suite_id")
    @classmethod
    def validate_suite_id(cls, value: str) -> str:
        return _safe_identifier(value, label="suite_id")

    @model_validator(mode="after")
    def unique_task_ids(self) -> "OfficeAcceptanceSuite":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("office acceptance task IDs must be unique")
        return self

    def task(self, task_id: str) -> OfficeTaskSpec:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(f"unknown office acceptance task: {task_id}")


class OfficeRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = OFFICE_ACCEPTANCE_SCHEMA_VERSION
    suite_id: str
    task_id: str
    task_spec_sha256: str
    run_id: str
    status: Literal["passed", "run_incomplete", "artifact_failed"]
    run_status: str
    environment: str
    artifact: OfficeArtifactVerification
    performance: RunPerformanceReport
    committed_cost_microusd: int = Field(ge=0)
    outstanding_cost_microusd: int = Field(ge=0)
    max_cost_microusd: int | None = Field(default=None, ge=1)
    pricing_version: str | None = None
    runner_stop_reason: str = ""
    artifact_capture_error: str | None = None


def load_office_suite(path: Path) -> OfficeAcceptanceSuite:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("office acceptance suite root must be a mapping")
    return OfficeAcceptanceSuite.model_validate(raw)


def _safe_members(
    artifact_bytes: bytes,
    *,
    max_bytes: int,
) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    if len(artifact_bytes) > max_bytes:
        raise OfficeAcceptanceError("artifact exceeds its declared size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(artifact_bytes))
    except zipfile.BadZipFile as exc:
        raise OfficeAcceptanceError(
            "artifact is not a valid OOXML ZIP package"
        ) from exc
    infos = archive.infolist()
    if len(infos) > MAX_PACKAGE_MEMBERS:
        archive.close()
        raise OfficeAcceptanceError("artifact contains too many package members")
    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if (
            path.is_absolute()
            or not info.filename
            or "\\" in info.filename
            or ":" in path.parts[0]
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            archive.close()
            raise OfficeAcceptanceError(
                "artifact contains an unsafe package path"
            )
        if info.flag_bits & 0x1:
            archive.close()
            raise OfficeAcceptanceError("encrypted OOXML members are unsupported")
        if info.file_size > MAX_PACKAGE_MEMBER_BYTES:
            archive.close()
            raise OfficeAcceptanceError(
                "artifact package member exceeds the safety limit"
            )
        total += info.file_size
        if total > MAX_PACKAGE_UNCOMPRESSED_BYTES:
            archive.close()
            raise OfficeAcceptanceError(
                "artifact package expands beyond the safety limit"
            )
        if info.filename in members:
            archive.close()
            raise OfficeAcceptanceError(
                "artifact contains duplicate package paths"
            )
        members[info.filename] = info
    if "[Content_Types].xml" not in members:
        archive.close()
        raise OfficeAcceptanceError("artifact has no OOXML content-type manifest")
    return archive, members


def _read_xml(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    name: str,
) -> ElementTree.Element:
    if name not in members:
        raise OfficeAcceptanceError(f"artifact is missing required OOXML part: {name}")
    value = archive.read(members[name])
    lowered = value.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise OfficeAcceptanceError("OOXML part contains a forbidden declaration")
    try:
        return ElementTree.fromstring(value)
    except ElementTree.ParseError as exc:
        raise OfficeAcceptanceError(f"OOXML part is malformed: {name}") from exc


def _check(name: str, passed: bool, success: str, failure: str) -> OfficeCheckResult:
    return OfficeCheckResult(
        name=name,
        passed=passed,
        detail=success if passed else failure,
    )


def _phrase_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "phrase"


def _verify_docx(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    expectation: DocxExpectation,
) -> list[OfficeCheckResult]:
    root = _read_xml(archive, members, "word/document.xml")
    paragraphs: list[tuple[str, str | None]] = []
    for paragraph in root.iter(f"{{{_W}}}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{{{_W}}}t":
                parts.append(node.text or "")
            elif node.tag == f"{{{_W}}}tab":
                parts.append("\t")
            elif node.tag in {f"{{{_W}}}br", f"{{{_W}}}cr"}:
                parts.append("\n")
        style_node = paragraph.find(
            f"./{{{_W}}}pPr/{{{_W}}}pStyle"
        )
        style = (
            style_node.attrib.get(f"{{{_W}}}val")
            if style_node is not None
            else None
        )
        text = "".join(parts)
        if text.strip():
            paragraphs.append((text, style))
    combined = "\n".join(text for text, _style in paragraphs)
    word_count = len(_WORD.findall(combined))
    checks = [
        _check(
            "minimum_paragraphs",
            len(paragraphs) >= expectation.min_paragraphs,
            "paragraph threshold met",
            "document has too few non-empty paragraphs",
        ),
        _check(
            "minimum_word_count",
            word_count >= expectation.min_word_count,
            "word-count threshold met",
            "document is below the minimum word count",
        ),
    ]
    if expectation.max_word_count is not None:
        checks.append(
            _check(
                "maximum_word_count",
                word_count <= expectation.max_word_count,
                "word-count ceiling met",
                "document exceeds the maximum word count",
            )
        )
    if expectation.forbid_repeated_spaces:
        checks.append(
            _check(
                "repeated_spaces",
                "  " not in combined,
                "document contained no repeated spaces",
                "document contained repeated spaces",
            )
        )
    if expectation.title is not None:
        first_text = paragraphs[0][0] if paragraphs else ""
        checks.append(
            _check(
                "title",
                first_text == expectation.title,
                "title matched exactly",
                "first non-empty paragraph did not match the title",
            )
        )
    if expectation.title_style is not None:
        first_style = paragraphs[0][1] if paragraphs else None
        checks.append(
            _check(
                "title_style",
                first_style == expectation.title_style,
                "title paragraph style matched",
                "title paragraph style did not match",
            )
        )
    folded = combined.casefold()
    for phrase in expectation.required_phrases:
        checks.append(
            _check(
                f"required_phrase:{_phrase_slug(phrase)}",
                phrase.casefold() in folded,
                "required phrase was present",
                "required phrase was absent",
            )
        )
    for index, expected in enumerate(expectation.exact_paragraphs):
        actual = paragraphs[index][0] if index < len(paragraphs) else None
        checks.append(
            _check(
                f"exact_paragraph:{index}",
                actual == expected,
                "paragraph matched exactly",
                "paragraph did not match exactly",
            )
        )
    return checks


def _shared_strings(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> list[str]:
    if "xl/sharedStrings.xml" not in members:
        return []
    root = _read_xml(archive, members, "xl/sharedStrings.xml")
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_S}}}t"))
        for item in root.iter(f"{{{_S}}}si")
    ]


def _workbook_sheets(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> dict[str, str]:
    workbook = _read_xml(archive, members, "xl/workbook.xml")
    relationships = _read_xml(
        archive,
        members,
        "xl/_rels/workbook.xml.rels",
    )
    targets = {
        relation.attrib.get("Id", ""): relation.attrib.get("Target", "")
        for relation in relationships.iter(f"{{{_PR}}}Relationship")
    }
    sheets: dict[str, str] = {}
    for sheet in workbook.iter(f"{{{_S}}}sheet"):
        name = sheet.attrib.get("name", "")
        relation_id = sheet.attrib.get(f"{{{_R}}}id", "")
        target = targets.get(relation_id, "")
        if not name or not target:
            raise OfficeAcceptanceError(
                "workbook contains an unresolved worksheet relationship"
            )
        if target.startswith("/"):
            normalized = posixpath.normpath(target.lstrip("/"))
        else:
            normalized = posixpath.normpath(posixpath.join("xl", target))
        if (
            normalized.startswith("../")
            or normalized == ".."
            or not normalized.startswith("xl/")
        ):
            raise OfficeAcceptanceError(
                "workbook contains an unsafe worksheet relationship"
            )
        if name.casefold() in {existing.casefold() for existing in sheets}:
            raise OfficeAcceptanceError(
                "workbook contains duplicate worksheet names"
            )
        sheets[name] = normalized
    return sheets


def _cell_value(
    cell: ElementTree.Element,
    shared: list[str],
) -> tuple[Any, str | None]:
    kind = cell.attrib.get("t", "")
    formula_node = cell.find(f"{{{_S}}}f")
    formula = formula_node.text if formula_node is not None else None
    if kind == "inlineStr":
        value = "".join(
            node.text or "" for node in cell.iter(f"{{{_S}}}t")
        )
        return value, formula
    value_node = cell.find(f"{{{_S}}}v")
    raw = value_node.text if value_node is not None else None
    if raw is None:
        return None, formula
    if kind == "s":
        try:
            return shared[int(raw)], formula
        except (ValueError, IndexError) as exc:
            raise OfficeAcceptanceError(
                "worksheet contains an invalid shared-string index"
            ) from exc
    if kind == "b":
        if raw not in {"0", "1"}:
            raise OfficeAcceptanceError(
                "worksheet contains an invalid boolean cell"
            )
        return raw == "1", formula
    if kind in {"str", "e", "d"}:
        return raw, formula
    try:
        return Decimal(raw), formula
    except InvalidOperation:
        return raw, formula


def _values_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float, Decimal)):
        if not isinstance(actual, (int, float, Decimal)):
            return False
        try:
            expected_decimal = Decimal(str(expected))
            actual_decimal = Decimal(str(actual))
        except InvalidOperation:
            return False
        if expected_decimal == actual_decimal:
            return True
        if (
            expected_decimal == expected_decimal.to_integral_value()
            and actual_decimal == actual_decimal.to_integral_value()
        ):
            return False
        expected_float = float(expected_decimal)
        actual_float = float(actual_decimal)
        return (
            math.isfinite(expected_float)
            and math.isfinite(actual_float)
            and expected_float == actual_float
        )
    return expected == actual


def _verify_xlsx(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    expectation: XlsxExpectation,
) -> list[OfficeCheckResult]:
    shared = _shared_strings(archive, members)
    workbook_sheets = _workbook_sheets(archive, members)
    checks: list[OfficeCheckResult] = []
    for sheet_name, expected_sheet in expectation.sheets.items():
        sheet_key = _phrase_slug(sheet_name)
        present = sheet_name in workbook_sheets
        checks.append(
            _check(
                f"sheet:{sheet_key}",
                present,
                "worksheet was present",
                "required worksheet was absent",
            )
        )
        if not present:
            for reference, expected_cell in expected_sheet.cells.items():
                if "value" in expected_cell.model_fields_set:
                    checks.append(
                        _check(
                            f"cell:{sheet_key}!{reference.casefold()}",
                            False,
                            "cell value matched",
                            "cell could not be read because the worksheet was absent",
                        )
                    )
                if expected_cell.formula is not None:
                    checks.append(
                        _check(
                            f"formula:{sheet_key}!{reference.casefold()}",
                            False,
                            "cell formula matched",
                            "formula could not be read because the worksheet was absent",
                        )
                    )
            continue
        worksheet = _read_xml(
            archive,
            members,
            workbook_sheets[sheet_name],
        )
        actual_cells = {
            cell.attrib["r"].upper(): _cell_value(cell, shared)
            for cell in worksheet.iter(f"{{{_S}}}c")
            if _CELL_REFERENCE.fullmatch(cell.attrib.get("r", "").upper())
        }
        for reference, expected_cell in expected_sheet.cells.items():
            actual_value, actual_formula = actual_cells.get(
                reference,
                (None, None),
            )
            if "value" in expected_cell.model_fields_set:
                checks.append(
                    _check(
                        f"cell:{sheet_key}!{reference.casefold()}",
                        _values_match(expected_cell.value, actual_value),
                        "cell value matched",
                        "cell value did not match",
                    )
                )
            if expected_cell.formula is not None:
                normalized_actual = (
                    actual_formula.strip().removeprefix("=")
                    if actual_formula is not None
                    else None
                )
                checks.append(
                    _check(
                        f"formula:{sheet_key}!{reference.casefold()}",
                        normalized_actual == expected_cell.formula,
                        "cell formula matched",
                        "cell formula did not match",
                    )
                )
    return checks


def verify_office_artifact(
    spec: OfficeArtifactSpec,
    artifact_bytes: bytes,
) -> OfficeArtifactVerification:
    """Parse and score one saved Office artifact without trusting app UI state."""

    digest = hashlib.sha256(artifact_bytes).hexdigest()
    try:
        archive, members = _safe_members(
            artifact_bytes,
            max_bytes=spec.max_bytes,
        )
        try:
            if spec.format == "docx":
                assert spec.docx is not None
                checks = _verify_docx(archive, members, spec.docx)
            else:
                assert spec.xlsx is not None
                checks = _verify_xlsx(archive, members, spec.xlsx)
        finally:
            archive.close()
    except OfficeAcceptanceError as exc:
        return OfficeArtifactVerification(
            format=spec.format,
            passed=False,
            sha256=digest,
            byte_count=len(artifact_bytes),
            error=str(exc),
        )
    return OfficeArtifactVerification(
        format=spec.format,
        passed=all(check.passed for check in checks),
        sha256=digest,
        byte_count=len(artifact_bytes),
        checks=checks,
    )


def _task_spec_sha256(task: OfficeTaskSpec) -> str:
    encoded = json.dumps(
        task.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_office_run_result(
    *,
    suite: OfficeAcceptanceSuite,
    task: OfficeTaskSpec,
    run: RunSnapshot,
    artifact_bytes: bytes,
    environment: str,
    performance: RunPerformanceReport | None = None,
    runner_stop_reason: str = "",
    artifact_capture_error: str | None = None,
) -> OfficeRunResult:
    """Combine the durable run and independent artifact proof into one result."""

    if task.task_id not in {candidate.task_id for candidate in suite.tasks}:
        raise ValueError("task does not belong to the supplied suite")
    if not _SAFE_ID.fullmatch(environment):
        raise ValueError("environment must be a safe public label")
    artifact = verify_office_artifact(task.artifact, artifact_bytes)
    if run.status is not RunStatus.COMPLETED:
        status = "run_incomplete"
    elif not artifact.passed:
        status = "artifact_failed"
    else:
        status = "passed"
    resolved_performance = performance or summarize_run_performance(run)
    resolved_performance = resolved_performance.model_copy(
        update={
            "provider_attempts": max(
                resolved_performance.provider_attempts,
                run.model_budget.provider_attempts,
            )
        }
    )
    return OfficeRunResult(
        suite_id=suite.suite_id,
        task_id=task.task_id,
        task_spec_sha256=_task_spec_sha256(task),
        run_id=run.run_id,
        status=status,
        run_status=run.status.value,
        environment=environment,
        artifact=artifact,
        performance=resolved_performance,
        committed_cost_microusd=run.model_budget.committed_cost_microusd,
        outstanding_cost_microusd=run.model_budget.outstanding_cost_microusd,
        max_cost_microusd=run.model_budget.max_cost_microusd,
        pricing_version=run.model_budget.pricing_version,
        runner_stop_reason=runner_stop_reason,
        artifact_capture_error=artifact_capture_error,
    )


def write_office_result(path: Path, result: OfficeRunResult) -> None:
    """Create one immutable public result file and refuse accidental overwrite."""

    if not path.parent.is_dir():
        raise ValueError("office result parent directory does not exist")
    data = (result.model_dump_json(indent=2) + "\n").encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
    except FileExistsError as exc:
        raise ValueError("office result already exists") from exc
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
