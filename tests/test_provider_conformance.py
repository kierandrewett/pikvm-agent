from __future__ import annotations

import asyncio
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.agent_models import ModelRequest, ModelResponse
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute
from pikvm_agent.harness.provider_conformance import (
    ProviderConformanceDecision,
    conformance_expectations,
    read_provider_conformance_health,
    render_conformance_case,
    run_provider_conformance,
    write_provider_conformance_report,
)


class QueueProvider:
    def __init__(
        self,
        name: str,
        decisions: list[dict[str, str]],
        *,
        latency_ms: int = 25,
    ) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.decisions = list(decisions)
        self.latency_ms = latency_ms
        self.requests: list[ModelRequest] = []
        self.active = 0
        self.max_active = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            return ModelResponse(
                provider=self.name,
                model=self.model,
                data=self.decisions.pop(0),
                usage={
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "private_provider_field": "must not persist",
                },
                latency_ms=self.latency_ms,
            )
        finally:
            self.active -= 1


class FailingProvider:
    name = "failing"
    model = "private-model"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError(
            "rate limit from private-provider-body do-not-persist"
        )


def _decision(expected: dict[str, str]) -> dict[str, str]:
    return {
        "screen_title": expected["screen_title"],
        "verification_code": expected["verification_code"],
        "primary_button_label": expected["primary_button_label"],
    }


def test_conformance_case_is_deterministic_and_keeps_answers_out_of_request(
    tmp_path: Path,
) -> None:
    expected = conformance_expectations(seed=104729, cases=1)[0]
    case = render_conformance_case(
        expected=expected,
        index=0,
        output_dir=tmp_path,
    )

    assert case.image_path.is_file()
    assert case.image_path.stat().st_size > 1_000
    assert case.image_size == (960, 540)
    request_text = case.prompt + json.dumps(case.metadata, sort_keys=True)
    assert expected["screen_title"] not in request_text
    assert expected["verification_code"] not in request_text
    assert expected["primary_button_label"] not in request_text
    assert "transcribe" in case.prompt.casefold()
    assert "provider-conformance:0" == case.run_id


@pytest.mark.asyncio
async def test_conformance_report_is_failure_inclusive_and_schema_valid(
    tmp_path: Path,
) -> None:
    expected = conformance_expectations(seed=104729, cases=2)
    exact = QueueProvider("exact", [_decision(item) for item in expected])
    inexact_decisions = [_decision(item) for item in expected]
    inexact_decisions[1]["verification_code"] += "X"
    inexact = QueueProvider("inexact", inexact_decisions)

    report = await run_provider_conformance(
        providers={
            "exact": exact,
            "inexact": inexact,
            "failing": FailingProvider(),
        },
        provider_metadata={
            "exact": {
                "kind": "codex_cli",
                "ready": True,
                "auth_mode": "saved_cli_login",
                "support_tier": "stable",
                "implementation_contract": "first_party",
                "credential_owner": "provider_cli",
                "interface": "Codex exec",
                "pixel_input": "Native image attachment",
                "structured_output": "Strict JSON Schema",
            },
            "inexact": {
                "kind": "gemini_api",
                "ready": True,
                "auth_mode": "api_key_env",
                "interface": "Gemini generateContent",
                "pixel_input": "Inline image data",
                "structured_output": "JSON Schema",
            },
            "failing": {
                "kind": "anthropic_api",
                "ready": True,
                "auth_mode": "api_key_env",
            },
            "unavailable": {
                "kind": "claude_cli",
                "ready": False,
                "auth_mode": "saved_cli_login",
                "error": "executable-not-found",
            },
        },
        provider_names=["exact", "inexact", "failing", "unavailable"],
        cases=2,
        seed=104729,
        concurrency=2,
        workspace=tmp_path,
        now=lambda: datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    )

    assert report.created_at == "2026-07-26T12:00:00+00:00"
    assert report.computer_target_contacted is False
    assert report.providers_selected == 4
    assert report.providers_exercised == 3
    assert report.providers_unavailable == 1
    assert report.calls_attempted == 6
    assert report.calls_schema_valid == 4
    assert report.calls_exact == 3
    assert report.calls_normalized_exact == 3
    assert report.calls_failed == 2
    assert report.exact_accuracy == 0.5

    by_name = {provider.name: provider for provider in report.providers}
    assert by_name["exact"].exact == 2
    assert by_name["exact"].support_tier == "stable"
    assert by_name["exact"].implementation_contract == "first_party"
    assert by_name["exact"].credential_owner == "provider_cli"
    assert by_name["exact"].median_latency_ms == 25
    assert by_name["exact"].usage_totals == {
        "input_tokens": 24,
        "output_tokens": 8,
    }
    assert by_name["inexact"].exact == 1
    assert by_name["failing"].failure_counts == {"rate-limited": 2}
    assert by_name["unavailable"].exercised is False
    assert by_name["unavailable"].readiness_error == "executable-not-found"
    assert exact.max_active <= 2
    assert all(
        request.output_schema
        == ProviderConformanceDecision.model_json_schema()
        for request in exact.requests
    )
    assert all(
        item.expected is not None
        and item.observed is not None
        and "private_provider_field" not in item.model_dump_json()
        for item in by_name["exact"].results
    )
    serialized = report.model_dump_json()
    assert "do-not-persist" not in serialized
    assert "private-provider-body" not in serialized
    assert "private-model" not in serialized

    destination = tmp_path / "latest.json"
    write_provider_conformance_report(destination, report)
    health = read_provider_conformance_health(
        destination,
        provider_names=[
            "exact",
            "inexact",
            "failing",
            "unavailable",
            "not-selected",
        ],
    )
    assert health["exact"] == {
        "conformance_status": "passed",
        "conformance_created_at": "2026-07-26T12:00:00+00:00",
        "conformance_cases_requested": 2,
        "conformance_calls_attempted": 2,
        "conformance_schema_valid": 2,
        "conformance_exact": 2,
        "conformance_normalized_exact": 2,
        "conformance_exact_accuracy": 1.0,
        "conformance_normalized_exact_accuracy": 1.0,
        "conformance_median_latency_ms": 25.0,
        "conformance_p95_latency_ms": 25.0,
        "conformance_failure_counts": {},
    }
    assert health["inexact"]["conformance_status"] == "degraded"
    assert health["failing"]["conformance_status"] == "failed"
    assert health["unavailable"]["conformance_status"] == "unavailable"
    assert health["not-selected"]["conformance_status"] == "not-in-report"
    assert "expected" not in json.dumps(health)
    assert "observed" not in json.dumps(health)


@pytest.mark.asyncio
async def test_conformance_rejects_unknown_provider_and_invalid_bounds(
    tmp_path: Path,
) -> None:
    expected = conformance_expectations(seed=7, cases=1)
    provider = QueueProvider("known", [_decision(expected[0])])

    with pytest.raises(ValueError, match="unknown providers: missing"):
        await run_provider_conformance(
            providers={"known": provider},
            provider_metadata={"known": {"ready": True}},
            provider_names=["missing"],
            cases=1,
            seed=7,
            concurrency=1,
            workspace=tmp_path,
        )
    with pytest.raises(ValueError, match="cases must be between 1 and 100"):
        await run_provider_conformance(
            providers={"known": provider},
            provider_metadata={"known": {"ready": True}},
            provider_names=["known"],
            cases=0,
            seed=7,
            concurrency=1,
            workspace=tmp_path,
        )
    with pytest.raises(
        ValueError,
        match="concurrency must be between 1 and 16",
    ):
        await run_provider_conformance(
            providers={"known": provider},
            provider_metadata={"known": {"ready": True}},
            provider_names=["known"],
            cases=1,
            seed=7,
            concurrency=17,
            workspace=tmp_path,
        )


def test_conformance_report_write_is_mode_0600_and_never_overwrites(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "provider-conformance.json"
    payload = {"schema_version": 1, "safe": True}

    write_provider_conformance_report(destination, payload)

    assert json.loads(destination.read_text()) == payload
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_provider_conformance_report(destination, payload)


def test_conformance_health_reader_is_safe_for_missing_or_invalid_report(
    tmp_path: Path,
) -> None:
    missing = read_provider_conformance_health(
        tmp_path / "missing.json",
        provider_names=["configured"],
    )
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(
        '{"providers":[{"name":"configured","model":"private-model"}]}'
    )
    invalid = read_provider_conformance_health(
        invalid_path,
        provider_names=["configured"],
    )

    assert missing == {
        "configured": {"conformance_status": "not-run"}
    }
    assert invalid == {
        "configured": {"conformance_status": "invalid-report"}
    }
    assert "private-model" not in json.dumps(invalid)


def test_conformance_cli_requires_explicit_provider_call_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pikvm_agent.harness import config as harness_config

    config = tmp_path / "harness.yaml"
    config.write_text("{}")
    loaded = False

    def unexpected_load(_path: Path) -> object:
        nonlocal loaded
        loaded = True
        raise AssertionError("configuration must not be loaded before consent")

    monkeypatch.setattr(
        harness_config,
        "load_harness_settings",
        unexpected_load,
    )

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "provider-conformance",
            "--config",
            str(config),
            "--out",
            str(tmp_path / "report.json"),
        ],
    )

    assert result.exit_code == 2
    assert "--allow-provider-calls" in result.output
    assert loaded is False


def test_conformance_cli_runs_selected_provider_and_writes_safe_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pikvm_agent.harness import config as harness_config
    from pikvm_agent.harness import (
        provider_conformance as conformance_module,
    )

    config = tmp_path / "harness.yaml"
    config.write_text("{}")
    destination = tmp_path / "report.json"
    expected = conformance_expectations(seed=104729, cases=2)
    provider = QueueProvider(
        "exact",
        [_decision(item) for item in expected],
    )
    settings = SimpleNamespace(
        providers={"exact": SimpleNamespace(model="exact-model")},
        provider_conformance_path=destination,
    )
    pool = SimpleNamespace(providers={"exact": provider})
    monkeypatch.setattr(
        harness_config,
        "load_harness_settings",
        lambda _path: settings,
    )
    monkeypatch.setattr(
        harness_config,
        "check_provider_prerequisites",
        lambda _settings: {
            "exact": {
                "kind": "codex_cli",
                "auth_mode": "saved_cli_login",
                "ready": True,
            }
        },
    )
    monkeypatch.setattr(
        harness_config,
        "build_model_pool",
        lambda _settings: pool,
    )

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "provider-conformance",
            "--config",
            str(config),
            "--provider",
            "exact",
            "--cases",
            "2",
            "--concurrency",
            "2",
            "--allow-provider-calls",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    report = json.loads(destination.read_text())
    assert summary["computer_target_contacted"] is False
    assert summary["calls_exact"] == 2
    assert report["providers"][0]["configured_model"] == "exact-model"
    assert report["providers"][0]["usage_totals"] == {
        "input_tokens": 24,
        "output_tokens": 8,
    }
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    reads = 0
    read_health = conformance_module.read_provider_conformance_health

    def count_health_reads(
        source: Path,
        *,
        provider_names: list[str],
    ) -> dict[str, dict[str, object]]:
        nonlocal reads
        reads += 1
        return read_health(source, provider_names=provider_names)

    monkeypatch.setattr(
        conformance_module,
        "read_provider_conformance_health",
        count_health_reads,
    )
    health_pool = ModelPool(
        providers={"exact": provider},
        routes={
            "reasoner": RoleRoute(providers=["exact"]),
            "controller": RoleRoute(providers=["exact"]),
            "verifier": RoleRoute(providers=["exact"]),
        },
        provider_metadata={
            "exact": {
                "kind": "codex_cli",
                "configured_model": "exact-model",
            }
        },
        provider_conformance_path=destination,
    )
    health = health_pool.health()["exact"]
    health_pool.health()
    assert health["configured_model"] == "exact-model"
    assert health["conformance_status"] == "passed"
    assert health["conformance_exact"] == 2
    assert reads == 1
