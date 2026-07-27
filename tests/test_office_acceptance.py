from __future__ import annotations

import io
import json
import stat
import zipfile
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness import bootstrap_windows, office_runner
from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.office_acceptance import (
    CellExpectation,
    OfficeAcceptanceSuite,
    build_office_run_result,
    load_office_suite,
    verify_office_artifact,
    write_office_result,
)
from pikvm_agent.harness.office_runner import (
    HttpManagedHarnessApi,
    _artifact_acceptance_result,
    _fresh_artifact_path,
    drive_managed_office_run,
    run_live_office_case,
)


def _archive(members: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return output.getvalue()


def _docx(paragraphs: list[tuple[str, str | None]]) -> bytes:
    body = []
    for text, style in paragraphs:
        properties = (
            f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        )
        body.append(
            f"<w:p>{properties}<w:r><w:t>{text}</w:t></w:r></w:p>"
        )
    return _archive(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
            "word/document.xml": (
                '<?xml version="1.0"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body>'
                + "".join(body)
                + "</w:body></w:document>"
            ),
        }
    )


def _xlsx() -> bytes:
    return _archive(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet.main+xml"/>'
                "</Types>"
            ),
            "xl/workbook.xml": (
                '<?xml version="1.0"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main" xmlns:r="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Quarterly Earnings" sheetId="1" '
                'r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<?xml version="1.0"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships"><Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
            "xl/sharedStrings.xml": (
                '<?xml version="1.0"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
                '2006/main"><si><t>Quarterly Earnings</t></si></sst>'
            ),
            "xl/worksheets/sheet1.xml": (
                '<?xml version="1.0"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData>'
                '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
                '<row r="3"><c r="A3" t="inlineStr"><is><t>Quarter</t></is>'
                "</c></row>"
                '<row r="4"><c r="A4" t="inlineStr"><is><t>Q1</t></is></c>'
                '<c r="B4"><v>125.50</v></c></row>'
                '<row r="8"><c r="A8" t="inlineStr"><is><t>Total</t></is></c>'
                '<c r="B8"><f>SUM(B4:B7)</f><v>500</v></c></row>'
                "</sheetData></worksheet>"
            ),
        }
    )


def _suite() -> OfficeAcceptanceSuite:
    return OfficeAcceptanceSuite.model_validate(
        {
            "schema_version": 1,
            "suite_id": "office-smoke-v1",
            "tasks": [
                {
                    "task_id": "word-essay",
                    "instruction_template": (
                        "Write the requested essay and save it to {artifact_path}."
                    ),
                    "artifact": {
                        "format": "docx",
                        "filename": "shakespeare-essay.docx",
                        "docx": {
                            "title": "Shakespeare and Human Choice",
                            "title_style": "Title",
                            "min_paragraphs": 3,
                            "min_word_count": 18,
                            "forbid_repeated_spaces": True,
                            "required_phrases": [
                                "Hamlet",
                                "Macbeth",
                                "The Tempest",
                            ],
                        },
                    },
                },
                {
                    "task_id": "quarterly-earnings",
                    "instruction_template": (
                        "Build the supplied workbook and save it to {artifact_path}."
                    ),
                    "artifact": {
                        "format": "xlsx",
                        "filename": "quarterly-earnings.xlsx",
                        "xlsx": {
                            "sheets": {
                                "Quarterly Earnings": {
                                    "cells": {
                                        "A1": {"value": "Quarterly Earnings"},
                                        "A3": {"value": "Quarter"},
                                        "A4": {"value": "Q1"},
                                        "B4": {"value": Decimal("125.5")},
                                        "A8": {"value": "Total"},
                                        "B8": {
                                            "value": Decimal("500"),
                                            "formula": "SUM(B4:B7)",
                                        },
                                    }
                                }
                            }
                        },
                    },
                },
            ],
        }
    )


def test_live_office_artifact_path_is_fresh_and_scoped_to_the_lab_workspace() -> None:
    path = _fresh_artifact_path(
        "quarterly-earnings.xlsx",
        nonce="0123456789abcdef",
    )

    assert path == (
        "C:/PiKVM-Harness/workspace/"
        "quarterly-earnings-0123456789abcdef.xlsx"
    )


async def test_live_office_run_stays_paused_until_artifact_visibility_exists() -> None:
    requests: list[tuple[str, dict[str, object] | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        return httpx.Response(200, json={"run_id": "office-paused"})

    async with httpx.AsyncClient(
        base_url="http://harness.invalid",
        transport=httpx.MockTransport(respond),
    ) as client:
        api = HttpManagedHarnessApi(client)
        result = await api.create("Create the workbook.")
        await api.start(result["run_id"])

    assert result == {"run_id": "office-paused"}
    assert requests == [
        (
            "/api/runs",
            {
                "task": "Create the workbook.",
                "auto_start": False,
            },
        ),
        ("/api/runs/office-paused/start", None),
    ]


async def test_skip_provision_reuses_installed_observer_with_fresh_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class LabBoundaryReached(RuntimeError):
        pass

    captured: dict[str, object] = {}

    def record_deploy(**kwargs: object) -> None:
        captured.update(kwargs)

    class StopBeforeLab:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> object:
            raise LabBoundaryReached

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(bootstrap_windows, "deploy", record_deploy)
    monkeypatch.setattr(office_runner, "RunningLab", StopBeforeLab)
    monkeypatch.setattr(office_runner, "allocate_lab_ports", object)
    repo = Path(__file__).parents[1]

    with pytest.raises(LabBoundaryReached):
        await run_live_office_case(
            endpoint="disposable.invalid:5900",
            harness_config=repo / "config.harness.example.yaml",
            suite_path=repo / "bench" / "office-acceptance-v1.yaml",
            task_id="excel-quarterly-earnings",
            output_dir=tmp_path / "office-run",
            artifact_url=None,
            skip_provision=True,
            keymap="en-us",
            password=None,
            username=None,
            max_continuation_cycles=1,
            max_run_time_s=1,
        )

    assert captured["reuse_installed"] is True
    assert captured["visible"] is False
    assert captured["artifact_url"] is None
    assert str(captured["file_path"]).startswith(
        "C:/PiKVM-Harness/workspace/quarterly-earnings-"
    )
    assert str(captured["file_path"]).endswith(".xlsx")


@pytest.mark.parametrize(
    "nonce",
    ["", "ABCDEF0123456789", "../0123456789abcdef", "0123456789abcde"],
)
def test_live_office_artifact_path_rejects_unsafe_nonce(nonce: str) -> None:
    with pytest.raises(ValueError, match="nonce"):
        _fresh_artifact_path("quarterly-earnings.xlsx", nonce=nonce)


@pytest.mark.parametrize(
    "filename",
    ["quarterly earnings.xlsx", "../earnings.xlsx", 'bad"name.xlsx'],
)
def test_live_office_artifact_path_rejects_unsafe_filename(
    filename: str,
) -> None:
    with pytest.raises(ValueError, match="observer file"):
        _fresh_artifact_path(filename, nonce="0123456789abcdef")


def test_docx_task_requires_a_valid_saved_artifact_not_a_done_claim() -> None:
    task = _suite().task("word-essay")
    artifact = _docx(
        [
            ("Shakespeare and Human Choice", "Title"),
            (
                "Hamlet turns hesitation into a dramatic study of choice and "
                "responsibility.",
                None,
            ),
            (
                "Macbeth and The Tempest show ambition, mercy, power, and "
                "consequence from sharply different perspectives.",
                None,
            ),
        ]
    )

    result = verify_office_artifact(task.artifact, artifact)

    assert result.passed is True
    assert result.format == "docx"
    assert result.byte_count == len(artifact)
    assert len(result.sha256) == 64
    assert all(check.passed for check in result.checks)
    assert task.render_instruction(r"C:\PiKVM-Harness\workspace\essay.docx").endswith(
        r"C:\PiKVM-Harness\workspace\essay.docx."
    )


def test_docx_verifier_reports_missing_content_without_echoing_document_text() -> None:
    task = _suite().task("word-essay")

    result = verify_office_artifact(
        task.artifact,
        _docx(
            [
                ("Wrong title", "Normal"),
                ("Hamlet appears, but the other requirements do not.", None),
            ]
        ),
    )

    assert result.passed is False
    assert {check.name for check in result.checks if not check.passed} >= {
        "title",
        "title_style",
        "minimum_paragraphs",
        "minimum_word_count",
        "required_phrase:macbeth",
        "required_phrase:the-tempest",
    }
    assert "Hamlet appears" not in result.model_dump_json()


def test_docx_verifier_rejects_repeated_spaces_without_echoing_document_text() -> None:
    task = _suite().task("word-essay")
    artifact = _docx(
        [
            ("Shakespeare and Human Choice", "Title"),
            (
                "Hamlet turns hesitation into a dramatic study of choice and "
                "responsibility.",
                None,
            ),
            (
                "Macbeth and The Tempest show ambition,  mercy, power, and "
                "consequence from sharply different perspectives.",
                None,
            ),
        ]
    )

    result = verify_office_artifact(task.artifact, artifact)

    repeated_spaces = next(
        check for check in result.checks if check.name == "repeated_spaces"
    )
    assert result.passed is False
    assert repeated_spaces.passed is False
    assert "ambition,  mercy" not in result.model_dump_json()


def test_xlsx_task_verifies_exact_cells_and_formulas_semantically() -> None:
    task = _suite().task("quarterly-earnings")

    result = verify_office_artifact(task.artifact, _xlsx())

    assert result.passed is True
    assert {check.name for check in result.checks} >= {
        "sheet:quarterly-earnings",
        "cell:quarterly-earnings!a1",
        "cell:quarterly-earnings!b4",
        "formula:quarterly-earnings!b8",
    }


def test_xlsx_cell_expectation_accepts_formula_only_but_not_an_empty_check() -> None:
    expectation = CellExpectation(formula="SUM(B4:B7)")

    assert expectation.value is None
    assert expectation.formula == "SUM(B4:B7)"
    with pytest.raises(ValueError, match="value or formula"):
        CellExpectation()


def test_office_verifier_fails_closed_on_corrupt_or_unsafe_ooxml() -> None:
    task = _suite().task("word-essay")
    unsafe = _archive(
        {
            "../word/document.xml": "<document/>",
            "[Content_Types].xml": "<Types/>",
        }
    )

    corrupt = verify_office_artifact(task.artifact, b"not-a-zip")
    traversal = verify_office_artifact(task.artifact, unsafe)

    assert corrupt.passed is False
    assert corrupt.error == "artifact is not a valid OOXML ZIP package"
    assert traversal.passed is False
    assert traversal.error == "artifact contains an unsafe package path"


def test_public_task_result_requires_both_run_completion_and_artifact_proof() -> None:
    suite = _suite()
    task = suite.task("quarterly-earnings")
    run = RunSnapshot(
        run_id="run-office-1",
        task="private task prose is not included in public output",
        status=RunStatus.COMPLETED,
    )
    run.model_budget.provider_attempts = 6
    run.model_budget.provider_attempt_limit = 40
    run.model_budget.committed_cost_microusd = 320_000
    run.model_budget.max_cost_microusd = 1_500_000
    run.record(
        "model.completed",
        role="controller",
        provider="fast-controller",
        model="vision-fast-v1",
        latency_ms=640,
    )
    run.record(
        "action.checkpointed",
        index=0,
        actions=[{"type": "click"}],
    )
    run.record("action.attempted", index=0)
    run.record("action.completed", index=0)
    run.record("run.completed", summary="model claimed completion")

    passed = build_office_run_result(
        suite=suite,
        task=task,
        run=run,
        artifact_bytes=_xlsx(),
        environment="disposable-windows-vm",
    )
    failed = build_office_run_result(
        suite=suite,
        task=task,
        run=run.model_copy(update={"status": RunStatus.PAUSED}),
        artifact_bytes=_xlsx(),
        environment="disposable-windows-vm",
    )

    assert passed.status == "passed"
    assert passed.artifact.passed is True
    assert passed.performance.provider_attempts == 6
    assert passed.performance.actions_completed == 1
    assert passed.performance.model_lanes[0].model == "vision-fast-v1"
    assert passed.committed_cost_microusd == 320_000
    assert passed.max_cost_microusd == 1_500_000
    assert "private task prose" not in passed.model_dump_json()
    assert failed.status == "run_incomplete"


def test_checked_in_office_suite_is_portable_and_cli_verifiable(
    tmp_path: Path,
) -> None:
    suite_path = Path(__file__).parents[1] / "bench" / "office-acceptance-v1.yaml"
    suite = load_office_suite(suite_path)
    task = suite.task("word-shakespeare-essay")
    artifact = _docx(
        [
            ("Shakespeare and Human Choice", "Title"),
            *[
                (
                    "Hamlet Macbeth King Lear The Tempest human choice "
                    + " ".join(f"analysis{index}-{word}" for word in range(110)),
                    None,
                )
                for index in range(6)
            ],
        ]
    )
    artifact_path = tmp_path / task.artifact.filename
    artifact_path.write_bytes(artifact)

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "verify-office-artifact",
            "--suite",
            str(suite_path),
            "--task-id",
            task.task_id,
            "--artifact",
            str(artifact_path),
        ],
    )

    assert {candidate.task_id for candidate in suite.tasks} == {
        "word-shakespeare-essay",
        "excel-quarterly-earnings",
    }
    assert "{artifact_path}" in task.instruction_template
    assert "C:\\" not in suite_path.read_text()
    assert result.exit_code == 0
    assert '"passed": true' in result.stdout


async def test_live_runner_continues_bounded_slices_but_never_approves() -> None:
    class Api:
        def __init__(self) -> None:
            self.continues = 0
            self.order: list[str] = []
            self.states = [
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.RUNNING,
                ),
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.PAUSED,
                ),
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.NEEDS_APPROVAL,
                    pending_approval={
                        "approval_id": "approval-1",
                        "risk": "communication_send",
                    },
                ),
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.NEEDS_APPROVAL,
                    pending_approval={
                        "approval_id": "approval-1",
                        "risk": "communication_send",
                    },
                ),
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.COMPLETED,
                ),
            ]

        async def create(self, task: str):
            assert task == "Create the workbook."
            self.order.append("create")
            return {"run_id": "office-live"}

        async def start(self, run_id: str):
            assert run_id == "office-live"
            self.order.append("start")
            return {"run_id": run_id}

        async def get(self, run_id: str):
            assert run_id == "office-live"
            return self.states.pop(0).model_dump(mode="json")

        async def continue_run(self, run_id: str):
            self.continues += 1
            return {"run_id": run_id}

        async def abort(self, run_id: str, _reason: str):
            raise AssertionError(f"completed run must not be aborted: {run_id}")

        async def performance(self, run_id: str):
            raise AssertionError("not used by the control-loop unit test")

    api = Api()
    statuses: list[str] = []
    created: list[str] = []

    async def on_created(run_id: str) -> None:
        created.append(run_id)
        api.order.append("artifact-visible")

    outcome = await drive_managed_office_run(
        api,
        instruction="Create the workbook.",
        max_continuation_cycles=5,
        max_run_time_s=60,
        status_sink=statuses.append,
        on_created=on_created,
        sleep=lambda _seconds: _no_sleep(),
        monotonic=lambda: 0,
    )

    assert outcome.run.status is RunStatus.COMPLETED
    assert outcome.continuation_cycles == 1
    assert outcome.stop_reason == "completed"
    assert api.continues == 1
    assert api.order == ["create", "artifact-visible", "start"]
    assert created == ["office-live"]
    assert statuses.count(
        "Approval is waiting in the operator UI; the runner cannot approve it."
    ) == 1


@pytest.mark.asyncio
async def test_office_http_client_requests_background_continuation() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run_id": "office-live"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://harness",
    ) as client:
        await HttpManagedHarnessApi(client).continue_run("office-live")

    assert len(requests) == 1
    assert requests[0].url.path == "/api/runs/office-live/continue"
    assert requests[0].url.params.get("background") == "true"


def test_office_runner_accepts_sanitized_verification_receipts() -> None:
    payload = RunSnapshot(
        run_id="office-live",
        task="task",
        status=RunStatus.PAUSED,
        verification_images=[
            {
                "revision": 1,
                "action_index": 0,
                "before_frame_id": 1,
                "after_frame_id": 2,
                "path": "/private/evidence.png",
            }
        ],
    ).model_dump(mode="json")
    payload["verification_images"][0].pop("path")

    run = office_runner._run_snapshot(payload)

    assert run.status is RunStatus.PAUSED
    assert run.verification_images == []


@pytest.mark.asyncio
async def test_office_runner_does_not_duplicate_background_continuation() -> None:
    class Api:
        def __init__(self) -> None:
            self.continues = 0
            self.states = [
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.PAUSED,
                    event_cursor=7,
                ),
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.PAUSED,
                    event_cursor=7,
                ),
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.PAUSED,
                    event_cursor=7,
                ),
                RunSnapshot(
                    run_id="office-live",
                    task="task",
                    status=RunStatus.COMPLETED,
                    event_cursor=8,
                ),
            ]

        async def create(self, _task: str):
            return {"run_id": "office-live"}

        async def start(self, _run_id: str):
            return {"run_id": "office-live"}

        async def get(self, _run_id: str):
            return self.states.pop(0).model_dump(mode="json")

        async def continue_run(self, _run_id: str):
            self.continues += 1
            return {"run_id": "office-live"}

        async def abort(self, _run_id: str, _reason: str):
            raise AssertionError("completed run must not be aborted")

        async def performance(self, _run_id: str):
            raise AssertionError("not used")

    api = Api()
    outcome = await drive_managed_office_run(
        api,
        instruction="Continue the task.",
        max_continuation_cycles=5,
        max_run_time_s=60,
        sleep=lambda _seconds: _no_sleep(),
        monotonic=lambda: 0,
    )

    assert outcome.run.status is RunStatus.COMPLETED
    assert api.continues == 1


async def _no_sleep() -> None:
    return None


async def test_live_runner_aborts_when_artifact_visibility_cannot_start() -> None:
    class Api:
        def __init__(self) -> None:
            self.abort_reason = ""

        async def create(self, _task: str):
            return {"run_id": "office-visibility-failed"}

        async def get(self, _run_id: str):
            raise AssertionError("invisible run must not enter the polling loop")

        async def continue_run(self, _run_id: str):
            raise AssertionError("invisible run must not continue")

        async def abort(self, _run_id: str, reason: str):
            self.abort_reason = reason
            return RunSnapshot(
                run_id="office-visibility-failed",
                task="task",
                status=RunStatus.ABORTED,
            ).model_dump(mode="json")

        async def performance(self, _run_id: str):
            raise AssertionError("not used")

    async def fail_visibility(_run_id: str) -> None:
        raise RuntimeError("synthetic observer outage")

    api = Api()
    with pytest.raises(RuntimeError, match="observer outage"):
        await drive_managed_office_run(
            api,
            instruction="Create the workbook.",
            max_continuation_cycles=2,
            max_run_time_s=60,
            on_created=fail_visibility,
            sleep=lambda _seconds: _no_sleep(),
            monotonic=lambda: 0,
        )

    assert api.abort_reason == (
        "Office acceptance runner stopped: artifact visibility unavailable"
    )


async def test_live_runner_stops_at_its_own_continuation_limit() -> None:
    class Api:
        def __init__(self) -> None:
            self.continues = 0
            self.aborts = 0

        async def create(self, _task: str):
            return {"run_id": "office-loop"}

        async def start(self, run_id: str):
            return {"run_id": run_id}

        async def get(self, _run_id: str):
            return RunSnapshot(
                run_id="office-loop",
                task="task",
                status=RunStatus.PAUSED,
                event_cursor=self.continues,
            ).model_dump(mode="json")

        async def continue_run(self, _run_id: str):
            self.continues += 1
            return {"run_id": "office-loop"}

        async def abort(self, _run_id: str, _reason: str):
            self.aborts += 1
            return RunSnapshot(
                run_id="office-loop",
                task="task",
                status=RunStatus.ABORTED,
            ).model_dump(mode="json")

        async def performance(self, _run_id: str):
            raise AssertionError("not used")

    api = Api()
    outcome = await drive_managed_office_run(
        api,
        instruction="Create the workbook.",
        max_continuation_cycles=2,
        max_run_time_s=60,
        sleep=lambda _seconds: _no_sleep(),
        monotonic=lambda: 0,
    )

    assert outcome.stop_reason == "continuation-cycle-limit"
    assert outcome.run.status is RunStatus.ABORTED
    assert outcome.continuation_cycles == 2
    assert api.continues == 2
    assert api.aborts == 1


async def test_live_runner_aborts_the_managed_run_when_its_deadline_expires() -> None:
    running = RunSnapshot(
        run_id="office-timeout",
        task="task",
        status=RunStatus.RUNNING,
    ).model_dump(mode="json")

    class Api:
        def __init__(self) -> None:
            self.abort_reason = ""

        async def create(self, _task: str):
            return {"run_id": "office-timeout"}

        async def start(self, run_id: str):
            return {"run_id": run_id}

        async def get(self, _run_id: str):
            return running

        async def continue_run(self, _run_id: str):
            raise AssertionError("a running task must not be continued")

        async def abort(self, _run_id: str, reason: str):
            self.abort_reason = reason
            return RunSnapshot(
                run_id="office-timeout",
                task="task",
                status=RunStatus.ABORTED,
            ).model_dump(mode="json")

        async def performance(self, _run_id: str):
            raise AssertionError("not used")

    times = iter((0.0, 2.0))
    api = Api()
    outcome = await drive_managed_office_run(
        api,
        instruction="Create the workbook.",
        max_continuation_cycles=2,
        max_run_time_s=1,
        sleep=lambda _seconds: _no_sleep(),
        monotonic=lambda: next(times),
    )

    assert outcome.stop_reason == "runner-timeout"
    assert outcome.run.status is RunStatus.ABORTED
    assert api.abort_reason == "Office acceptance runner stopped: runner-timeout"


def test_public_office_result_is_immutable_and_contains_no_task_prose(
    tmp_path: Path,
) -> None:
    suite = _suite()
    task = suite.task("quarterly-earnings")
    run = RunSnapshot(
        run_id="immutable-result",
        task="private instruction",
        status=RunStatus.COMPLETED,
    )
    result = build_office_run_result(
        suite=suite,
        task=task,
        run=run,
        artifact_bytes=_xlsx(),
        environment="disposable-windows-vm",
    )
    destination = tmp_path / "result.json"

    write_office_result(destination, result)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert "private instruction" not in destination.read_text()
    with pytest.raises(ValueError, match="already exists"):
        write_office_result(destination, result)


def test_artifact_acceptance_payload_contains_only_host_evidence() -> None:
    suite = _suite()
    task = suite.task("quarterly-earnings")
    run = RunSnapshot(
        run_id="office-evidence",
        task="private task prose",
        status=RunStatus.COMPLETED,
    )
    result = build_office_run_result(
        suite=suite,
        task=task,
        run=run,
        artifact_bytes=_xlsx(),
        environment="disposable-windows-vm",
    )

    payload = _artifact_acceptance_result(task, result)

    assert payload["state"] == "passed"
    assert payload["artifact_format"] == "xlsx"
    assert payload["checks_passed"] == payload["checks_total"]
    assert payload["checks_total"] > 0
    assert payload["sha256"] == result.artifact.sha256
    assert "private task prose" not in str(payload)
    assert "C:/PiKVM-Harness" not in str(payload)
