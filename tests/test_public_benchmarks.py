from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from PIL import Image
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.agent_models import ModelRequest, ModelResponse
from pikvm_agent.harness.payload_shape_benchmark import (
    evaluate_payload_shape_cases,
    generate_payload_shape_cases,
)
from pikvm_agent.harness.public_benchmarks import run_screenspot_pro


class FixedGrounder:
    name = "fixed-grounder"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.role == "controller"
        assert request.image_path is not None
        assert "empty recycle bin" in request.prompt
        return ModelResponse(
            provider=self.name,
            model="fixture-model",
            data={
                "result": "positive",
                "point": [0.5376953125, 0.1045138889],
                "confidence": 0.9,
            },
            usage={
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 20,
            },
            latency_ms=17,
        )


class MissGrounder:
    name = "miss-grounder"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider=self.name,
            model="first-pass",
            data={
                "result": "positive",
                "point": [0.1, 0.1],
                "confidence": 0.8,
            },
            usage={"input_tokens": 40, "output_tokens": 10},
            latency_ms=11,
        )


class CorrectingVerifier:
    name = "correcting-verifier"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert "crosshair" in request.prompt
        assert request.image_path is not None
        assert Path(request.image_path).name.endswith("-candidate.png")
        return ModelResponse(
            provider=self.name,
            model="second-pass",
            data={
                "verdict": "miss",
                "corrected_point": [0.55, 0.55],
                "confidence": 0.95,
            },
            usage={"input_tokens": 60, "output_tokens": 15},
            latency_ms=13,
        )


def test_central_benchmark_scorecard_links_resolve_and_json_decodes() -> None:
    repo = Path(__file__).parents[1]
    scorecard = repo / "bench" / "README.md"
    local_targets = {
        match
        for match in re.findall(r"\]\(([^)]+)\)", scorecard.read_text())
        if "://" not in match and not match.startswith("#")
    }

    assert local_targets
    for target in sorted(local_targets):
        resolved = (scorecard.parent / target).resolve()
        assert resolved.is_relative_to(repo.resolve())
        assert resolved.is_file(), target
        if resolved.suffix == ".json":
            assert isinstance(json.loads(resolved.read_text()), dict), target


def test_historical_audit_narrative_matches_the_checked_coverage_ledger() -> None:
    repo = Path(__file__).parents[1]
    scorecard = (repo / "bench" / "README.md").read_text()
    section = scorecard.split("## Historical production-use failure audit", 1)[
        1
    ].split("## Exact Windows guest, foreground, and focus identity", 1)[0]
    coverage = json.loads(
        (repo / "bench" / "historical_pikvm_coverage.json").read_text()
    )
    counts = coverage["status_counts"]

    assert (
        f"{counts['covered_local']} are locally covered, "
        f"{counts['partial']} are partial, and {counts['open']} are open"
    ) in section


def test_precise_multiscale_ocr_evidence_is_honest_and_fail_closed() -> None:
    report = json.loads(
        (
            Path(__file__).parents[1]
            / "bench"
            / "results"
            / "2026-07-26"
            / "ocr"
            / "tesseract-precise-multiscale-n1000.json"
        ).read_text()
    )
    general = report["general_profile"]
    precise = report["precise_known-intent_profile"]
    policy = report["runtime_policy"]

    assert report["corpus"]["cases"] == 1000
    assert general["selected_normalized_exact"] == 569
    assert precise["selected_normalized_exact"] == 569
    assert precise["exact_candidate_matches"] == 645
    assert precise["additional_exact_candidates"] == 31
    assert precise["selected_text_regressions"] == 0
    assert precise["p95_latency_ms"] > general["p95_latency_ms"]
    assert policy["general_screen_ocr_uses_precise_profile"] is False
    assert policy["known_intended_text_readback_uses_precise_profile"] is True
    assert policy["nearest_guess_allowed"] is False
    assert policy["commit_authority"] is False
    assert report["target_contacted"] is False


def test_hybrid_candidate_union_evidence_is_honest_and_fail_closed() -> None:
    report = json.loads(
        (
            Path(__file__).parents[1]
            / "bench"
            / "results"
            / "2026-07-26"
            / "ocr"
            / "hybrid-known-intent-candidate-union-n1000.json"
        ).read_text()
    )
    union = report["known_intent_candidate_union"]
    scope = report["evaluation_scope"]

    assert report["cases"] == 1000
    assert union["matches"] == 827
    assert union["by_tier"]["routine"] == {
        "cases": 800,
        "matches": 776,
        "rate": 0.97,
    }
    assert union["by_tier"]["stress"] == {
        "cases": 200,
        "matches": 51,
        "rate": 0.255,
    }
    assert union["known_intent_only"] is True
    assert union["nearest_guess_allowed"] is False
    assert union["commit_authority"] is False
    assert report["safe_commit_authority"] is False
    assert scope["candidate_union_reconstructed_from_paired_completed_runs"] is True
    assert scope["hybrid_provider_executed"] is False
    assert scope["computer_target_contacted"] is False
    assert scope["nearest_guess_allowed"] is False
    assert [source["sha256"] for source in report["source_reports"]] == [
        "12d28404461e81a551c12e776f0a30f0f8b190a4a76a38e98c03cf5bfc3aa7f1",
        "7aedd4763ad495a3df78ca4aacc0bd00e49a212802fd6831e54ee92d7031d5bb",
    ]


def test_isolated_client_launch_evidence_does_not_claim_a_task_run() -> None:
    report = json.loads(
        (
            Path(__file__).parents[1]
            / "bench"
            / "results"
            / "2026-07-26"
            / "safety"
            / "isolated-managed-client-launch.json"
        ).read_text()
    )

    assert report["cases"] == 4
    assert report["passed"] == 4
    assert report["baseline"] == {
        "client": "codex",
        "effective_inventory_safe": False,
        "managed_count": 0,
        "active_pikvm_classification": "raw",
    }
    assert [result["client"] for result in report["results"]] == [
        "codex",
        "claude",
        "opencode",
        "gemini",
    ]
    assert all(result["safe"] for result in report["results"])
    assert all(
        result["modifies_persisted_config"] is False
        for result in report["results"]
    )
    safety = report["safety"]
    assert safety["computer_target_contacted"] is False
    assert safety["mcp_server_launched"] is False
    assert safety["model_called"] is False
    assert safety["coding_client_task_run"] is False
    assert safety["production_registration_modified"] is False
    opencode = report["results"][2]
    assert opencode["native_resolved_config_executed"] is True
    assert opencode["pure_mode"] is True
    assert opencode["default_deny_permissions_verified"] is True
    assert opencode["client_owned_oauth_linked_without_copy"] is True
    gemini = report["results"][3]
    assert gemini["native_effective_settings_loader_executed"] is True
    assert gemini["system_mcp_catalog_and_allowlist_verified"] is True
    assert gemini["admin_policy_path_and_content_verified"] is True
    assert gemini["admin_policy_enforcement_executed"] is False
    assert gemini["authenticated_profile_used"] is False
    provider = report["gemini_provider_sandbox_regression"]
    assert provider["file_admin_controls_ignored_by_installed_client"] is True
    assert provider["policy_enforcement_executed"] is False
    assert provider["model_called"] is False


def test_managed_smoke_lab_evidence_separates_contract_from_live_run() -> None:
    report = json.loads(
        (
            Path(__file__).parents[1]
            / "bench"
            / "results"
            / "2026-07-26"
            / "harness"
            / "managed-smoke-lab-contract.json"
        ).read_text()
    )

    assert report["contracts"] == {
        "total": 24,
        "passed": 24,
        "failed": 0,
    }
    smoke = report["slices"]["managed_smoke_lab"]
    assert smoke["interface_test_store"] == "in-memory"
    assert smoke["runtime_store_constructed"] == "sqlite"
    assert smoke["sqlite_request_executed"] is False
    assert smoke["deterministic_provider_calls"] == 3
    assert smoke["deterministic_provider_successes"] == 3
    assert smoke["external_provider_calls"] == 0

    validation = report["validation"]
    assert validation["focused_acceptance"] == {
        "selected_tests": 44,
        "passed": 44,
        "failed": 0,
        "includes_public_claim_integrity_gate": True,
        "includes_scorecard_drift_gate": True,
    }
    repository = validation["repository_suite"]
    assert repository["completed"] is False
    assert repository["failures_observed_before_blockers"] == 0
    assert repository["passed_count_claimed"] is False
    package = validation["package_rebuild"]
    assert package["completed"] is False
    assert package["network_used"] is False
    assert package["prior_wheel_current_inspector"] == "rejected-as-stale"

    live = report["live_attempt"]
    assert live["broker_decision"] == "rejected-before-process-creation"
    assert live["listener_opened"] is False
    assert live["outer_client_started"] is False
    assert live["mcp_process_started"] is False
    assert live["external_provider_called"] is False
    assert live["task_started"] is False

    safety = report["safety"]
    assert safety["vnc_contact"] is False
    assert safety["pikvm_contact"] is False
    assert safety["daemon_contact"] is False
    assert safety["production_registration_modified"] is False


async def test_screenspot_pro_scores_official_xyxy_annotation(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    annotations = dataset / "annotations"
    image_dir = dataset / "images" / "common_windows"
    annotations.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    Image.new("RGB", (5120, 1440), "white").save(image_dir / "screen.png")
    (annotations / "windows_common_windows.json").write_text(
        json.dumps(
            [
                {
                    "img_filename": "common_windows/screen.png",
                    "bbox": [2626, 138, 2880, 163],
                    "instruction": "empty recycle bin",
                    "id": "windows_common_windows_0",
                    "application": "windows_common",
                    "platform": "windows",
                    "img_size": [5120, 1440],
                    "ui_type": "text",
                    "group": "OS",
                }
            ]
        ),
        encoding="utf-8",
    )

    report = await run_screenspot_pro(
        FixedGrounder(),
        dataset_dir=dataset,
        output_dir=tmp_path / "results",
        suite_revision="dbe00114",
        dataset_revision="fixture",
        limit=1,
        seed=7,
        jobs=1,
    )

    assert report.suite == "screenspot-pro"
    assert report.schema_version == 5
    assert report.cases_evaluated == 1
    assert report.correct == 1
    assert report.accuracy == 1.0
    assert report.verifier_mode == "none"
    assert report.actionable_cases == 1
    assert report.abstained_cases == 0
    assert report.actionable_accuracy == 1.0
    assert report.by_platform["windows"].accuracy == 1.0
    assert report.by_ui_type["text"].accuracy == 1.0
    assert report.results[0].target_bbox == (2626, 138, 2880, 163)
    assert report.results[0].predicted_point == (2753, 151)
    assert report.results[0].click_error_pixels == 0.5
    assert report.results[0].failure_kind is None
    assert report.results[0].usage == {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
    }
    assert report.usage_totals == {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
    }
    assert (tmp_path / "results" / "report.json").is_file()


async def test_screenspot_verifier_is_veto_only_by_default(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    annotations = dataset / "annotations"
    image_dir = dataset / "images" / "app"
    annotations.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    Image.new("RGB", (100, 100), "white").save(image_dir / "screen.png")
    (annotations / "app.json").write_text(
        json.dumps(
            [
                {
                    "img_filename": "app/screen.png",
                    "bbox": [50, 50, 60, 60],
                    "instruction": "target",
                    "id": "case",
                    "application": "app",
                    "platform": "windows",
                    "img_size": [100, 100],
                    "ui_type": "icon",
                    "group": "OS",
                }
            ]
        ),
        encoding="utf-8",
    )

    report = await run_screenspot_pro(
        MissGrounder(),
        verifier_provider=CorrectingVerifier(),
        dataset_dir=dataset,
        output_dir=tmp_path / "results",
        suite_revision="suite",
        dataset_revision="dataset",
        limit=1,
        jobs=1,
    )

    assert report.initial_correct == 0
    assert report.correct == 0
    assert report.verifier_mode == "veto"
    assert report.actionable_cases == 0
    assert report.abstained_cases == 1
    assert report.actionable_accuracy is None
    assert report.model_calls == 2
    assert report.results[0].initial_point == (10, 10)
    assert report.results[0].predicted_point is None
    assert report.results[0].verifier_suggested_point == (55, 55)
    assert report.results[0].correction_applied is False
    assert report.results[0].usage == {
        "input_tokens": 40,
        "output_tokens": 10,
    }
    assert report.results[0].verifier_usage == {
        "input_tokens": 60,
        "output_tokens": 15,
    }
    assert report.usage_totals == {
        "input_tokens": 100,
        "output_tokens": 25,
    }


async def test_screenspot_verifier_correction_requires_explicit_mode(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    annotations = dataset / "annotations"
    image_dir = dataset / "images" / "app"
    annotations.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    Image.new("RGB", (100, 100), "white").save(image_dir / "screen.png")
    (annotations / "app.json").write_text(
        json.dumps(
            [
                {
                    "img_filename": "app/screen.png",
                    "bbox": [50, 50, 60, 60],
                    "instruction": "target",
                    "id": "case",
                    "application": "app",
                    "platform": "windows",
                    "img_size": [100, 100],
                    "ui_type": "icon",
                    "group": "OS",
                }
            ]
        ),
        encoding="utf-8",
    )

    report = await run_screenspot_pro(
        MissGrounder(),
        verifier_provider=CorrectingVerifier(),
        verifier_mode="correct",
        dataset_dir=dataset,
        output_dir=tmp_path / "results",
        suite_revision="suite",
        dataset_revision="dataset",
        limit=1,
        jobs=1,
    )

    assert report.verifier_mode == "correct"
    assert report.correct == 1
    assert report.actionable_cases == 1
    assert report.actionable_accuracy == 1.0
    assert report.results[0].predicted_point == (55, 55)
    assert report.results[0].verifier_suggested_point == (55, 55)
    assert report.results[0].correction_applied is True


def test_cli_exposes_live_screenspot_pro_benchmark() -> None:
    result = CliRunner().invoke(
        app,
        ["harness", "screenspot-pro", "--help"],
    )

    assert result.exit_code == 0
    assert "--config" in result.stdout
    assert "--provider" in result.stdout
    assert "--verifier-provider" in result.stdout
    assert "--verifier-mode" in result.stdout
    assert "--dataset" in result.stdout
    assert "--suite-revision" in result.stdout


async def test_screenspot_subset_only_requires_selected_images(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    annotations = dataset / "annotations"
    image_dir = dataset / "images" / "app"
    annotations.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    Image.new("RGB", (100, 100), "white").save(image_dir / "available.png")
    records = [
        {
            "img_filename": "app/available.png",
            "bbox": [50, 50, 60, 60],
            "instruction": "target",
            "id": "a",
            "application": "app",
            "platform": "windows",
            "img_size": [100, 100],
            "ui_type": "text",
            "group": "OS",
        },
        {
            "img_filename": "app/not-downloaded.png",
            "bbox": [10, 10, 20, 20],
            "instruction": "other target",
            "id": "b",
            "application": "app",
            "platform": "windows",
            "img_size": [100, 100],
            "ui_type": "icon",
            "group": "OS",
        },
    ]
    (annotations / "app.json").write_text(json.dumps(records), encoding="utf-8")

    report = await run_screenspot_pro(
        FixedGrounder(),
        dataset_dir=dataset,
        output_dir=tmp_path / "results",
        suite_revision="suite",
        dataset_revision="dataset",
        limit=1,
        seed=0,
        jobs=1,
    )

    assert report.cases_discovered == 2
    assert [result.case_id for result in report.results] == ["a"]


def test_osworld_public_scorecard_denominators_match_preserved_reports() -> None:
    repo = Path(__file__).parents[1]
    result_root = repo / "bench" / "results" / "2026-07-24" / "osworld"
    summary = json.loads((result_root / "summary.json").read_text())
    clean = summary["current_clean_diagnostic"]
    clean_reports = [
        json.loads((result_root / relative).read_text())
        for relative in clean["reports"]
    ]

    assert clean["cases"] == len(clean_reports) == 4
    assert all(report["official_score"] == 1.0 for report in clean_reports)
    assert all(report["harness_status"] == "completed" for report in clean_reports)
    assert clean["wall_clock_ms_total"] == sum(
        report["performance"]["wall_clock_ms"] for report in clean_reports
    )
    assert clean["model_active_ms_total"] == sum(
        report["performance"]["model_active_ms"] for report in clean_reports
    )
    assert clean["model_completions"] == sum(
        lane["latency"]["samples"]
        for report in clean_reports
        for lane in report["performance"]["model_lanes"]
    )
    assert clean["actions_attempted"] == sum(
        report["performance"]["actions_attempted"] for report in clean_reports
    )
    assert clean["actions_completed"] == sum(
        report["performance"]["actions_completed"] for report in clean_reports
    )

    scored = summary["all_scored_attempts"]
    scored_reports = [
        json.loads((result_root / item["path"]).read_text())
        for item in scored["reports"]
    ]
    assert scored["attempts"] == len(scored_reports) == 9
    assert scored["official_goal_state_passes"] == sum(
        report["official_score"] == 1.0 for report in scored_reports
    )
    assert scored["harness_completed_and_official_passed"] == sum(
        report["official_score"] == 1.0
        and report["harness_status"] == "completed"
        for report in scored_reports
    )

    unscored = summary["unscored_attempts"]
    assert unscored["count"] == len(unscored["evidence"]) == 6
    assert all((result_root / path).is_file() for path in unscored["evidence"])


def test_current_osworld_scorecard_extends_prior_denominators_honestly() -> None:
    repo = Path(__file__).parents[1]
    result_root = repo / "bench" / "results" / "2026-07-25" / "osworld"
    summary = json.loads((result_root / "summary.json").read_text())
    current = summary["current_nine_task_diagnostic"]
    current_reports = [
        json.loads((result_root / relative).resolve().read_text())
        for relative in current["reports"]
    ]

    assert current["cases"] == len(current_reports) == 9
    assert current["official_passes"] == sum(
        report["official_score"] == 1.0 for report in current_reports
    )
    assert current["harness_completed_and_official_passed"] == sum(
        report["official_score"] == 1.0
        and report["harness_status"] == "completed"
        for report in current_reports
    )
    assert current["wall_clock_ms_total"] == sum(
        report["performance"]["wall_clock_ms"] for report in current_reports
    )
    assert current["model_active_ms_total"] == sum(
        report["performance"]["model_active_ms"] for report in current_reports
    )
    assert current["model_completions"] == sum(
        lane["latency"]["samples"]
        for report in current_reports
        for lane in report["performance"]["model_lanes"]
    )
    assert current["actions_attempted"] == sum(
        report["performance"]["actions_attempted"] for report in current_reports
    )
    assert current["actions_completed"] == sum(
        report["performance"]["actions_completed"] for report in current_reports
    )

    prior_root = repo / "bench" / "results" / "2026-07-24" / "osworld"
    prior = json.loads((prior_root / "summary.json").read_text())
    scored = summary["all_scored_attempts"]
    new_scored = [
        json.loads((result_root / item["path"]).read_text())
        for item in scored["new_reports"]
    ]
    assert scored["attempts"] == (
        prior["all_scored_attempts"]["attempts"] + len(new_scored)
    )
    assert scored["official_goal_state_passes"] == (
        prior["all_scored_attempts"]["official_goal_state_passes"]
        + sum(report["official_score"] == 1.0 for report in new_scored)
    )
    assert scored["harness_completed_and_official_passed"] == (
        prior["all_scored_attempts"]["harness_completed_and_official_passed"]
        + sum(
            report["official_score"] == 1.0
            and report["harness_status"] == "completed"
            for report in new_scored
        )
    )

    unscored = summary["unscored_attempts"]
    assert unscored["count"] == (
        prior["unscored_attempts"]["count"] + len(unscored["new_evidence"])
    )
    assert all((result_root / path).is_file() for path in unscored["new_evidence"])


def test_latest_osworld_scorecard_retains_failed_remediation_attempts() -> None:
    repo = Path(__file__).parents[1]
    result_root = repo / "bench" / "results" / "2026-07-28" / "osworld"
    summary = json.loads((result_root / "summary.json").read_text())
    current = summary["current_nine_task_diagnostic"]
    current_reports = [
        json.loads((result_root / relative).resolve().read_text())
        for relative in current["reports"]
    ]

    assert current["cases"] == len(current_reports) == 9
    assert current["official_passes"] == sum(
        report["official_score"] == 1.0 for report in current_reports
    ) == 7
    assert current["harness_completed_and_official_passed"] == sum(
        report["official_score"] == 1.0
        and report["harness_status"] == "completed"
        for report in current_reports
    ) == 7
    assert current["wall_clock_ms_total"] == sum(
        report["performance"]["wall_clock_ms"] for report in current_reports
    )
    assert current["model_active_ms_total"] == sum(
        report["performance"]["model_active_ms"] for report in current_reports
    )
    assert current["model_completions"] == sum(
        lane["latency"]["samples"]
        for report in current_reports
        for lane in report["performance"]["model_lanes"]
    )
    assert current["provider_attempts"] == sum(
        report["performance"].get("provider_attempts")
        or sum(
            lane["latency"]["samples"]
            for lane in report["performance"]["model_lanes"]
        )
        for report in current_reports
    )
    assert current["actions_attempted"] == sum(
        report["performance"]["actions_attempted"] for report in current_reports
    )
    assert current["actions_completed"] == sum(
        report["performance"]["actions_completed"] for report in current_reports
    )

    passing_reports = [
        report
        for report in current_reports
        if report["official_score"] == 1.0
        and report["harness_status"] == "completed"
    ]
    passing = summary["passing_subset"]
    assert passing["cases"] == len(passing_reports) == 7
    assert passing["wall_clock_ms_total"] == sum(
        report["performance"]["wall_clock_ms"] for report in passing_reports
    )
    assert passing["model_active_ms_total"] == sum(
        report["performance"]["model_active_ms"] for report in passing_reports
    )
    assert passing["actions_attempted"] == sum(
        report["performance"]["actions_attempted"] for report in passing_reports
    )
    assert passing["actions_completed"] == sum(
        report["performance"]["actions_completed"] for report in passing_reports
    )

    prior = json.loads(
        (
            repo
            / "bench"
            / "results"
            / "2026-07-25"
            / "osworld"
            / "summary.json"
        ).read_text()
    )
    scored = summary["all_scored_attempts"]
    new_scored = [
        json.loads((result_root / item["path"]).read_text())
        for item in scored["new_reports"]
    ]
    assert scored["attempts"] == (
        prior["all_scored_attempts"]["attempts"] + len(new_scored)
    ) == 37
    assert scored["official_goal_state_passes"] == (
        prior["all_scored_attempts"]["official_goal_state_passes"]
        + sum(report["official_score"] == 1.0 for report in new_scored)
    ) == 8
    assert scored["harness_completed_and_official_passed"] == (
        prior["all_scored_attempts"]["harness_completed_and_official_passed"]
        + sum(
            report["official_score"] == 1.0
            and report["harness_status"] == "completed"
            for report in new_scored
        )
    ) == 7

    iteration = summary["latest_remediation_iteration"]
    retained_reports = []
    for item in iteration["reports"]:
        report_path = result_root / item["path"]
        report = json.loads(report_path.read_text())
        retained_reports.append(report)
        assert hashlib.sha256(report_path.read_bytes()).hexdigest() == item["sha256"]
        assert item["harness_status"] == report["harness_status"]
        assert item["official_score"] == report["official_score"]
        assert item["wall_clock_ms"] == report["performance"]["wall_clock_ms"]
        assert item["model_active_ms"] == report["performance"]["model_active_ms"]
        assert item["actions_attempted"] == report["performance"][
            "actions_attempted"
        ]
        assert item["actions_completed"] == report["performance"][
            "actions_completed"
        ]
    assert iteration["attempts"] == len(retained_reports) == 4
    assert iteration["harness_completed_and_official_passed"] == 1
    assert iteration["wall_clock_ms_total"] == sum(
        report["performance"]["wall_clock_ms"] for report in retained_reports
    )
    assert iteration["model_active_ms_total"] == sum(
        report["performance"]["model_active_ms"] for report in retained_reports
    )
    assert iteration["provider_attempts"] == sum(
        report["performance"]["provider_attempts"] for report in retained_reports
    )
    assert iteration["actions_attempted"] == sum(
        report["performance"]["actions_attempted"] for report in retained_reports
    )
    assert iteration["actions_completed"] == sum(
        report["performance"]["actions_completed"] for report in retained_reports
    )

    latest = iteration["latest_success"]
    assert latest["official_score"] == 1.0
    assert latest["harness_status"] == "completed"
    assert latest["separate_return_commits"] == 2
    assert latest["source_state_sha256"] == (
        "200493a762739058d9bc357890a6da3773aeeaf7fae9b1a209537439b5482045"
    )
    assert latest["source_state_retained_locally"] is True
    assert [
        (
            receipt["source_event_sequence"],
            receipt["requested_characters"],
            receipt["issued_characters"],
            receipt["observed_characters"],
            receipt["status"],
            receipt["proof_state"],
            receipt["exact_readback_sha256_match"],
            receipt["emitted_exactly_once"],
        )
        for receipt in latest["exact_terminal_drafts"]
    ] == [
        (150, 68, 68, 68, "verified_exact", "exact_visual_readback", True, True),
        (197, 62, 62, 62, "verified_exact", "exact_visual_readback", True, True),
    ]

    unscored = summary["unscored_attempts"]
    assert unscored["count"] == prior["unscored_attempts"]["count"] == 11
    assert unscored["new_evidence"] == []


def test_osworld_model_comparison_is_traceable_to_live_reports() -> None:
    repo = Path(__file__).parents[1]
    result_root = repo / "bench" / "results" / "2026-07-25" / "osworld"
    comparison = json.loads((result_root / "model-comparison.json").read_text())

    assert comparison["official_passes"] == 0
    assert {run["route"] for run in comparison["runs"]} == {
        "all_codex",
        "all_claude",
        "claude_reasoner_codex_controller_verifier",
        "claude_reasoner_codex_primary_claude_fallback",
    }
    for run in comparison["runs"]:
        report = json.loads((result_root / run["report"]).read_text())
        assert run["harness_status"] == report["harness_status"]
        assert run["official_score"] == report["official_score"]
        assert run["wall_clock_ms"] == report["performance"]["wall_clock_ms"]
        assert run["model_active_ms"] == report["performance"]["model_active_ms"]
        assert run["actions_completed"] == report["performance"]["actions_completed"]
        assert run["actions_checkpointed"] == report["performance"][
            "actions_checkpointed"
        ]

    mixed = next(
        run
        for run in comparison["runs"]
        if run["route"] == "claude_reasoner_codex_controller_verifier"
    )
    assert mixed["progress_actions_completed"] == 11
    assert mixed["observation_only_actions_completed"] == 5
    assert mixed["progress_action_ratio"] == 11 / 16


def test_hid_payload_shape_report_names_checked_regressions() -> None:
    repo = Path(__file__).parents[1]
    report = json.loads(
        (
            repo
            / "bench"
            / "results"
            / "2026-07-25"
            / "safety"
            / "hid-payload-shape-gate-2026-07-26.json"
        ).read_text()
    )

    assert report["counts"] == {
        "passed": 1000,
        "total": 1000,
        "expected_reject": 800,
        "expected_allow": 200,
        "false_negative_count": 0,
        "false_positive_count": 0,
        "test_nodes": 2,
    }
    assert report["target_contacted"] is False
    evaluated = evaluate_payload_shape_cases(generate_payload_shape_cases())
    assert report["corpus_sha256"] == evaluated["corpus_sha256"]
    assert report["counts"]["passed"] == (
        evaluated["unsafe_refused"] + evaluated["safe_allowed"]
    )
    assert report["families"] == evaluated["families"]
    for node in report["regression_tests"]:
        relative, test_name = node.split("::", 1)
        source = (repo / relative).read_text()
        assert f"def {test_name}(" in source
