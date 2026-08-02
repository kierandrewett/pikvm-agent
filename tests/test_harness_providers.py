from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pikvm_agent.harness.agent_models import (
    ControllerDecision,
    ModelRequest,
    ModelResponse,
    PlanDecision,
)
from pikvm_agent.harness.codex_app_server import CodexAppServerTurnResult
from pikvm_agent.harness.model_pool import (
    ModelPool,
    ModelPoolError,
    RoleRoute,
    _provider_failure_class,
)
from pikvm_agent.harness.providers import (
    AnthropicApiProvider,
    AsyncioProcessRunner,
    CommandBearerAuth,
    ClaudeCodeProvider,
    CodexAppServerProvider,
    CodexExecProvider,
    EnvironmentHeaderAuth,
    GeminiApiProvider,
    GeminiCliProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProcessResult,
    SubprocessJsonProvider,
)


def request() -> ModelRequest:
    return ModelRequest(
        role="reasoner",
        prompt="Plan this task.",
        output_schema=PlanDecision.model_json_schema(),
        run_id="run_1",
    )


def test_provider_timeout_wording_maps_to_timeout_class() -> None:
    assert (
        _provider_failure_class(
            RuntimeError("provider command timed out after 60.0s")
        )
        == "timeout"
    )


class InvalidProvider:
    name = "invalid"

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider=self.name, model="bad", data={"summary": "missing fields"}
        )


class ValidProvider:
    name = "valid"

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider=self.name,
            model="good",
            data={
                "summary": "A valid plan",
                "steps": ["Do one thing"],
                "success_criteria": ["It is visibly complete"],
            },
        )


class RaisingProvider:
    name = "raising"

    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        self.calls += 1
        raise RuntimeError(self.message)


class RecoveringProvider:
    name = "recovering"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("rate-limited private retry detail")
        return ModelResponse(
            provider=self.name,
            model="recovered",
            data={
                "summary": "Recovered",
                "steps": ["Continue"],
                "success_criteria": ["Schema-valid output"],
            },
        )


class TextAndCommitProvider:
    name = "text-and-commit"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            provider=self.name,
            model="unsafe-controller",
            data={
                "outcome": "act",
                "intent": "Prepare the terminal query, then submit it.",
                "actions": [
                    {
                        "type": "type_text",
                        "text": "find video.mp4",
                        "context": "terminal",
                    },
                    {"type": "key", "keys": ["ENTER"]},
                ],
                "expected_evidence": [
                    "The exact query is visible before it is submitted."
                ],
            },
        )


class FocusTextAndCommitProvider:
    name = "focus-text-and-commit"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            provider=self.name,
            model="unsafe-controller",
            data={
                "outcome": "act",
                "intent": "Focus, type, and commit without verification.",
                "actions": [
                    {"type": "click", "x": 400, "y": 300},
                    {
                        "type": "type_text",
                        "text": "find video.mp4",
                        "context": "terminal",
                    },
                    {"type": "key", "keys": ["ENTER"]},
                ],
                "expected_evidence": ["The command completed."],
            },
        )


class SafeSettingsLaunchProvider:
    name = "safe-settings-launch"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            provider=self.name,
            model="safe-controller",
            data={
                "outcome": "act",
                "intent": "Open the read-only Windows About settings page.",
                "actions": [
                    {"type": "key", "keys": ["META", "R"]},
                    {"type": "wait", "ms": 150},
                    {
                        "type": "type_text",
                        "text": "ms-settings:about",
                        "context": "field",
                        "verification": "exact",
                    },
                    {"type": "key", "keys": ["ENTER"]},
                    {
                        "type": "wait_for_stable_screen",
                        "stable_ms": 300,
                        "timeout_ms": 3000,
                    },
                ],
                "expected_evidence": ["The About settings page is visible."],
            },
        )


@pytest.mark.asyncio
async def test_model_pool_falls_back_only_before_schema_valid_output() -> None:
    pool = ModelPool(
        providers={"invalid": InvalidProvider(), "valid": ValidProvider()},
        routes={"reasoner": RoleRoute(providers=["invalid", "valid"])},
    )

    plan, response = await pool.complete(request(), PlanDecision)

    assert plan.summary == "A valid plan"
    assert response.provider == "valid"
    assert pool.health()["invalid"]["failures"] == 1
    assert pool.health()["valid"]["successes"] == 1


@pytest.mark.asyncio
async def test_model_pool_honors_an_explicit_byo_provider() -> None:
    invalid = InvalidProvider()
    valid = ValidProvider()
    pool = ModelPool(
        providers={"invalid": invalid, "valid": valid},
        routes={"reasoner": RoleRoute(providers=["invalid", "valid"])},
    )

    plan, response = await pool.complete(
        request(),
        PlanDecision,
        preferred_provider="valid",
    )

    assert plan.summary == "A valid plan"
    assert response.provider == "valid"
    assert pool.health()["invalid"]["calls"] == 0
    assert pool.route_names(
        "reasoner",
        preferred_provider="valid",
    ) == ["valid"]


@pytest.mark.asyncio
async def test_model_pool_honors_an_ordered_per_run_route_with_fallback() -> None:
    invalid = InvalidProvider()
    valid = ValidProvider()
    pool = ModelPool(
        providers={"invalid": invalid, "valid": valid},
        routes={"reasoner": RoleRoute(providers=["valid"])},
    )

    plan, response = await pool.complete(
        request(),
        PlanDecision,
        provider_route=["invalid", "valid"],
    )

    assert plan.summary == "A valid plan"
    assert response.provider == "valid"
    assert pool.health()["invalid"]["calls"] >= 1
    assert pool.health()["valid"]["calls"] == 1
    assert pool.route_names(
        "reasoner",
        provider_route=["invalid", "valid"],
    ) == ["invalid", "valid"]
    with pytest.raises(ValueError, match="mutually exclusive"):
        pool.route_names(
            "reasoner",
            preferred_provider="valid",
            provider_route=["invalid", "valid"],
        )


@pytest.mark.asyncio
async def test_model_pool_downgrades_text_plus_commit_to_a_visible_safe_draft() -> None:
    provider = TextAndCommitProvider()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={"controller": RoleRoute(providers=[provider.name])},
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def record(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))

    decision, _response = await pool.complete(
        ModelRequest(
            role="controller",
            prompt="Choose the next bounded action.",
            output_schema=ControllerDecision.model_json_schema(),
            run_id="run-controller-safe-draft",
        ),
        ControllerDecision,
        on_event=record,
    )

    assert provider.calls == 1
    assert [action.type for action in decision.actions] == ["type_text"]
    assert decision.actions[0].text == "find video.mp4"
    assert decision.expected_evidence == [
        "The exact drafted text is visibly present in the focused input "
        "without being submitted."
    ]
    assert [kind for kind, _data in events] == [
        "provider_started",
        "provider_request_sent",
        "provider_output_received",
        "provider_validating",
        "provider_schema_safety_downgrade",
        "provider_completed",
    ]
    assert events[4][1] == {
        "provider": provider.name,
        "route_index": 0,
        "attempt": 1,
        "reason": "text-active-follow-up-separated",
        "preserved_actions": 1,
        "dropped_actions": 1,
        "dropped_action_types": ["key"],
    }


@pytest.mark.asyncio
async def test_model_pool_preserves_verified_windows_settings_launch() -> None:
    provider = SafeSettingsLaunchProvider()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={"controller": RoleRoute(providers=[provider.name])},
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def record(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))

    decision, _response = await pool.complete(
        ModelRequest(
            role="controller",
            prompt="Choose the next bounded action.",
            output_schema=ControllerDecision.model_json_schema(),
            run_id="run-controller-safe-settings",
        ),
        ControllerDecision,
        on_event=record,
    )

    assert provider.calls == 1
    assert [action.type for action in decision.actions] == [
        "key",
        "wait",
        "type_text",
        "key",
        "wait_for_stable_screen",
    ]
    assert "provider_schema_safety_downgrade" not in {
        kind for kind, _data in events
    }


@pytest.mark.asyncio
async def test_model_pool_never_downgrades_unverified_focus_plus_text() -> None:
    provider = FocusTextAndCommitProvider()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={"controller": RoleRoute(providers=[provider.name])},
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def record(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))

    with pytest.raises(ModelPoolError, match="invalid-structured-output"):
        await pool.complete(
            ModelRequest(
                role="controller",
                prompt="Choose the next bounded action.",
                output_schema=ControllerDecision.model_json_schema(),
                run_id="run-controller-unsafe-focus",
            ),
            ControllerDecision,
            on_event=record,
        )

    assert provider.calls == 2
    assert "provider_schema_safety_downgrade" not in {
        kind for kind, _data in events
    }


@pytest.mark.asyncio
async def test_model_pool_streams_each_attempt_repair_and_fallback() -> None:
    pool = ModelPool(
        providers={"invalid": InvalidProvider(), "valid": ValidProvider()},
        routes={"reasoner": RoleRoute(providers=["invalid", "valid"])},
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def record(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))

    await pool.complete(request(), PlanDecision, on_event=record)

    assert [kind for kind, _data in events] == [
        "provider_started",
        "provider_request_sent",
        "provider_output_received",
        "provider_validating",
        "provider_schema_repair",
        "provider_started",
        "provider_request_sent",
        "provider_output_received",
        "provider_validating",
        "provider_failed",
        "provider_failover",
        "provider_started",
        "provider_request_sent",
        "provider_output_received",
        "provider_validating",
        "provider_completed",
    ]
    assert events[0][1] == {
        "provider": "invalid",
        "model": "",
        "route_index": 0,
        "attempt": 1,
        "repair": False,
    }
    repair = events[4][1]
    assert repair["validation_error_types"] == ["missing"]
    assert repair["validation_error_locations"] == [
        "steps",
        "success_criteria",
    ]
    assert repair["validation_error_messages"] == ["Field required"]
    assert events[11][1]["provider"] == "valid"
    assert events[11][1]["route_index"] == 1


@pytest.mark.asyncio
async def test_model_pool_skips_known_unready_provider_and_exposes_route_status() -> None:
    unavailable = RaisingProvider("must not execute")
    pool = ModelPool(
        providers={"unavailable": unavailable, "valid": ValidProvider()},
        routes={
            "reasoner": RoleRoute(providers=["unavailable", "valid"]),
        },
        provider_metadata={
            "unavailable": {
                "kind": "anthropic_api",
                "ready": False,
                "credential": "env-missing",
                "error": "credential-env-missing",
                "routes": [{"role": "reasoner", "position": 1}],
            },
            "valid": {
                "kind": "codex_cli",
                "ready": True,
                "credential": "owned-by-cli",
                "routes": [{"role": "reasoner", "position": 2}],
            },
        },
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def record(kind: str, data: dict[str, object]) -> None:
        events.append((kind, data))

    _plan, response = await pool.complete(
        request(),
        PlanDecision,
        on_event=record,
    )
    health = pool.health()

    assert response.provider == "valid"
    assert unavailable.calls == 0
    assert events[0] == (
        "provider_skipped",
        {
            "provider": "unavailable",
            "route_index": 0,
            "reason": "not-ready",
            "error": "credential-env-missing",
        },
    )
    assert health["unavailable"]["ready"] is False
    assert health["unavailable"]["kind"] == "anthropic_api"
    assert health["unavailable"]["credential"] == "env-missing"
    assert health["unavailable"]["routes"] == [
        {"role": "reasoner", "position": 1}
    ]
    assert health["valid"]["ready"] is True


@pytest.mark.asyncio
async def test_model_pool_cools_down_failure_without_exposing_provider_text() -> None:
    failing = RaisingProvider("private-provider-body do-not-leak")
    pool = ModelPool(
        providers={"raising": failing, "valid": ValidProvider()},
        routes={
            "reasoner": RoleRoute(providers=["raising", "valid"]),
        },
        failure_cooldowns={"raising": 60.0},
    )
    second_events: list[tuple[str, dict[str, object]]] = []

    await pool.complete(request(), PlanDecision)

    async def record(kind: str, data: dict[str, object]) -> None:
        second_events.append((kind, data))

    await pool.complete(request(), PlanDecision, on_event=record)
    health = pool.health()["raising"]

    assert failing.calls == 1
    assert second_events[0][0] == "provider_skipped"
    assert second_events[0][1]["reason"] == "cooldown"
    assert health["last_error"] == "provider-error"
    assert health["last_error_class"] == "provider-error"
    assert health["cooldown_until"]
    assert "do-not-leak" not in str(health)
    assert "do-not-leak" not in str(second_events)


@pytest.mark.asyncio
async def test_model_pool_retries_provider_after_cooldown_expires() -> None:
    clock = [100.0]
    recovering = RecoveringProvider()
    pool = ModelPool(
        providers={"recovering": recovering},
        routes={"reasoner": RoleRoute(providers=["recovering"])},
        failure_cooldowns={"recovering": 10.0},
        monotonic=lambda: clock[0],
    )

    with pytest.raises(ModelPoolError, match="rate-limited"):
        await pool.complete(request(), PlanDecision)
    with pytest.raises(ModelPoolError, match="cooldown"):
        await pool.complete(request(), PlanDecision)

    clock[0] += 10.0
    plan, response = await pool.complete(request(), PlanDecision)

    assert plan.summary == "Recovered"
    assert response.provider == "recovering"
    assert recovering.calls == 2
    health = pool.health()["recovering"]
    assert health["successes"] == 1
    assert health["consecutive_failures"] == 0
    assert health["cooldown_until"] is None


@pytest.mark.asyncio
async def test_model_pool_all_fail_error_exposes_only_failure_classes() -> None:
    pool = ModelPool(
        providers={
            "first": RaisingProvider("secret response alpha"),
            "second": RaisingProvider("authentication-failed secret beta"),
        },
        routes={"reasoner": RoleRoute(providers=["first", "second"])},
        failure_cooldowns={"first": 0.0, "second": 0.0},
    )

    with pytest.raises(ModelPoolError) as caught:
        await pool.complete(request(), PlanDecision)

    message = str(caught.value)
    assert "first=provider-error" in message
    assert "second=authentication-failed" in message
    assert "alpha" not in message
    assert "beta" not in message


class RecordingRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        argv: list[str],
        stdin: str,
        cwd: str | None,
        timeout_s: float,
        env: dict[str, str],
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": argv,
                "stdin": stdin,
                "cwd": cwd,
                "timeout_s": timeout_s,
                "env": env,
            }
        )
        return ProcessResult(returncode=0, stdout=self.stdout, stderr="")


class CodexRecordingRunner(RecordingRunner):
    async def run(self, **kwargs: Any) -> ProcessResult:
        argv = kwargs["argv"]
        schema_index = argv.index("--output-schema") + 1
        self.schema = json.loads(Path(argv[schema_index]).read_text())
        return await super().run(**kwargs)


class CodexAppServerRecordingSession:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def complete(self, **kwargs: object) -> CodexAppServerTurnResult:
        self.calls.append(kwargs)
        return CodexAppServerTurnResult(
            text=json.dumps(self.response),
            model="gpt-5.6-luna",
            usage={
                "input_tokens": 80,
                "cached_input_tokens": 20,
                "output_tokens": 24,
            },
            latency_ms=321,
        )

    async def aclose(self) -> None:
        self.closed = True


class ClaudeRecordingRunner(RecordingRunner):
    async def run(self, **kwargs: Any) -> ProcessResult:
        argv = kwargs["argv"]
        schema_index = argv.index("--json-schema") + 1
        self.schema = json.loads(argv[schema_index])
        self.workspace_files = sorted(
            path.name for path in Path(kwargs["cwd"]).iterdir()
        )
        self.stdin = kwargs["stdin"]
        return await super().run(**kwargs)


class GeminiRecordingRunner(RecordingRunner):
    async def run(self, **kwargs: Any) -> ProcessResult:
        argv = kwargs["argv"]
        policy_index = argv.index("--admin-policy") + 1
        self.policy = Path(argv[policy_index]).read_text()
        self.system_settings = json.loads(
            Path(kwargs["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text()
        )
        self.system_defaults = json.loads(
            Path(kwargs["env"]["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"]).read_text()
        )
        self.workspace_files = sorted(
            path.name for path in Path(kwargs["cwd"]).iterdir()
        )
        return await super().run(**kwargs)


@pytest.mark.asyncio
async def test_cancelled_cli_provider_kills_its_child_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingProcess:
        returncode = None

        def __init__(self) -> None:
            self.killed = False
            self.waited = False

        async def communicate(self, _stdin: bytes) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return -9

    process = BlockingProcess()

    async def create_process(*_argv: str, **_kwargs: Any) -> BlockingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    task = asyncio.create_task(
        AsyncioProcessRunner().run(
            argv=["provider"],
            stdin="request",
            cwd=None,
            timeout_s=60,
            env={},
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_cli_timeout_kills_process_before_waiting_for_pipe_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DescendantHoldingPipeProcess:
        returncode = None
        pid = None

        def __init__(self) -> None:
            self.killed = asyncio.Event()
            self.waited = False

        async def communicate(self, _stdin: bytes) -> tuple[bytes, bytes]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Models a descendant that inherited stdout/stderr. Cancelling
                # communicate alone cannot close those pipes.
                await self.killed.wait()
            return b"", b""

        def kill(self) -> None:
            self.killed.set()
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            await self.killed.wait()
            return -9

    process = DescendantHoldingPipeProcess()
    create_kwargs: dict[str, Any] = {}

    async def create_process(*_argv: str, **kwargs: Any) -> DescendantHoldingPipeProcess:
        create_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(RuntimeError, match="timed out after 0.0s"):
        await asyncio.wait_for(
            AsyncioProcessRunner().run(
                argv=["provider"],
                stdin="request",
                cwd=None,
                timeout_s=0.01,
                env={},
            ),
            timeout=0.5,
        )

    assert process.killed.is_set()
    assert create_kwargs["start_new_session"] is True


@pytest.mark.asyncio
async def test_claude_cli_surfaces_structured_stdout_failure_diagnostics() -> None:
    runner = RecordingRunner(
        json.dumps(
            {
                "is_error": True,
                "terminal_reason": "api_error",
                "api_error_status": 401,
                "result": "Invalid API key do-not-leak",
            }
        )
    )

    async def failed_run(**kwargs: Any) -> ProcessResult:
        await RecordingRunner.run(runner, **kwargs)
        return ProcessResult(
            returncode=1, stdout=runner.stdout, stderr=""
        )

    runner.run = failed_run  # type: ignore[method-assign]
    provider = ClaudeCodeProvider(
        name="claude-account",
        runner=runner,
        inherited_env=["PATH", "HOME"],
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.complete(request())
    message = str(caught.value)
    assert "terminal_reason=api_error" in message
    assert "api_error_status=401" in message
    assert "authentication-failed" in message
    assert "do-not-leak" not in message


@pytest.mark.asyncio
async def test_claude_cli_zero_exit_error_does_not_leak_provider_text() -> None:
    runner = RecordingRunner(
        json.dumps(
            {
                "is_error": True,
                "subtype": "error_during_execution",
                "result": "private provider response do-not-leak",
            }
        )
    )
    provider = ClaudeCodeProvider(
        name="claude-account",
        runner=runner,
        inherited_env=["PATH", "HOME"],
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.complete(request())

    message = str(caught.value)
    assert "provider-error" in message
    assert "do-not-leak" not in message


@pytest.mark.asyncio
async def test_gemini_cli_reuses_dedicated_oauth_profile_without_tools_or_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "current-screen.png"
    image.write_bytes(b"frame")
    profile = tmp_path / "gemini-profile"
    profile.mkdir()
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("HOME", "/private/home")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    monkeypatch.setenv("TEST_GEMINI_PROFILE", str(profile))
    runner = GeminiRecordingRunner(
        json.dumps(
            {
                "session_id": "session-1",
                "response": json.dumps(
                    {
                        "summary": "Gemini plan",
                        "steps": ["Inspect"],
                        "success_criteria": ["Visible evidence"],
                    }
                ),
                "stats": {
                    "models": {
                        "gemini-test": {
                            "tokens": {
                                "prompt": 12,
                                "candidates": 7,
                            }
                        }
                    }
                },
            }
        )
    )
    provider = GeminiCliProvider(
        name="gemini-oauth",
        model="gemini-test",
        profile_home_env="TEST_GEMINI_PROFILE",
        runner=runner,
    )

    response = await provider.complete(
        request().model_copy(update={"image_path": str(image)})
    )

    call = runner.calls[0]
    argv = call["argv"]
    assert argv[:3] == ["gemini", "--prompt", ""]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert argv[argv.index("--extensions") + 1] == "none"
    assert argv[argv.index("--model") + 1] == "gemini-test"
    assert "--admin-policy" in argv
    assert "--yolo" not in argv
    assert runner.workspace_files == ["screen.png"]
    assert "@screen.png" in call["stdin"]
    assert "OUTPUT JSON SCHEMA" in call["stdin"]
    assert runner.policy == (
        '[[rule]]\n'
        'toolName = "*"\n'
        'decision = "deny"\n'
        "priority = 999\n"
        'deny_message = "Model tools are disabled in the PiKVM harness provider lane."\n'
    )
    assert runner.system_settings == {
        "mcp": {"allowed": []},
        "mcpServers": {},
        "security": {
            "disableYoloMode": True,
            "disableAlwaysAllow": True,
            "enablePermanentToolApproval": False,
        },
        "skills": {"enabled": False},
        "context": {
            "fileName": "PIKVM_HARNESS_CONTEXT_DISABLED.md",
            "includeDirectoryTree": False,
            "loadMemoryFromIncludeDirectories": False,
        },
        "hooksConfig": {"enabled": False},
    }
    assert runner.system_defaults == {}
    assert call["env"]["GEMINI_CLI_HOME"] == str(profile)
    assert call["env"]["GEMINI_CLI_SURFACE"] == "pikvm-harness"
    assert "TEST_GEMINI_PROFILE" not in call["env"]
    assert "HOME" not in call["env"]
    assert "OPENAI_API_KEY" not in call["env"]
    assert response.data["summary"] == "Gemini plan"
    assert response.usage["models"]["gemini-test"]["tokens"]["prompt"] == 12


@pytest.mark.asyncio
async def test_gemini_cli_requires_an_existing_dedicated_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_GEMINI_PROFILE", raising=False)
    provider = GeminiCliProvider(
        name="gemini-oauth",
        profile_home_env="TEST_GEMINI_PROFILE",
        runner=RecordingRunner("{}"),
    )

    with pytest.raises(
        RuntimeError,
        match="dedicated profile environment is not configured",
    ):
        await provider.complete(request())

    monkeypatch.setenv(
        "TEST_GEMINI_PROFILE",
        str(tmp_path / "missing-profile"),
    )
    with pytest.raises(
        RuntimeError,
        match="dedicated profile directory is unavailable",
    ):
        await provider.complete(request())

    monkeypatch.setenv("TEST_GEMINI_PROFILE", str(Path.home()))
    with pytest.raises(
        RuntimeError,
        match="must not reuse the normal user home",
    ):
        await provider.complete(request())


@pytest.mark.asyncio
async def test_gemini_cli_structured_error_does_not_leak_provider_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "gemini-profile"
    profile.mkdir()
    monkeypatch.setenv("TEST_GEMINI_PROFILE", str(profile))
    runner = RecordingRunner(
        json.dumps(
            {
                "error": {
                    "type": "AuthError",
                    "message": "Authentication failed do-not-leak",
                }
            }
        )
    )
    provider = GeminiCliProvider(
        name="gemini-oauth",
        profile_home_env="TEST_GEMINI_PROFILE",
        runner=runner,
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.complete(request())

    assert "authentication-failed" in str(caught.value)
    assert "do-not-leak" not in str(caught.value)


@pytest.mark.asyncio
async def test_subprocess_provider_uses_argv_not_shell_and_parses_nested_json() -> None:
    payload = {
        "response": (
            "```json\n"
            + json.dumps(
                {
                    "summary": "CLI plan",
                    "steps": ["Inspect"],
                    "success_criteria": ["Visible evidence"],
                }
            )
            + "\n```"
        )
    }
    runner = RecordingRunner(json.dumps(payload))
    provider = SubprocessJsonProvider(
        name="oauth-cli",
        model="subscription-model",
        argv=["provider-cli", "--headless", "--format", "json"],
        response_path="response",
        runner=runner,
        inherited_env=["PATH"],
    )

    response = await provider.complete(request())

    assert response.data["summary"] == "CLI plan"
    assert runner.calls[0]["argv"] == [
        "provider-cli",
        "--headless",
        "--format",
        "json",
    ]
    assert "Plan this task." in runner.calls[0]["stdin"]
    assert "success_criteria" in runner.calls[0]["stdin"]
    assert runner.calls[0]["env"].keys() == {"PATH"}


@pytest.mark.asyncio
async def test_generic_subprocess_does_not_inherit_home_or_credentials_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("HOME", "/private/home")
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    runner = RecordingRunner(
        json.dumps(
            {
                "summary": "isolated",
                "steps": ["Inspect"],
                "success_criteria": ["Visible evidence"],
            }
        )
    )
    provider = SubprocessJsonProvider(
        name="generic",
        model="local",
        argv=["bridge"],
        runner=runner,
    )

    await provider.complete(request())

    assert runner.calls[0]["env"] == {"PATH": "/safe/bin"}


@pytest.mark.asyncio
async def test_codex_exec_provider_reuses_cli_auth_in_isolated_read_only_mode(
    tmp_path: Path,
) -> None:
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"frame")
    model_request = request().model_copy(update={"image_path": str(image)})
    runner = CodexRecordingRunner(
        json.dumps(
            {
                "summary": "Codex plan",
                "steps": ["Inspect"],
                "success_criteria": ["Visible evidence"],
            }
        )
    )
    provider = CodexExecProvider(
        name="codex-oauth",
        model="account-default",
        runner=runner,
        inherited_env=["PATH", "HOME", "CODEX_HOME"],
    )

    response = await provider.complete(model_request)

    argv = runner.calls[0]["argv"]
    workdir = Path(runner.calls[0]["cwd"])
    assert argv[:2] == ["codex", "exec"]
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert workdir.name == "workspace"
    assert argv[argv.index("-c") + 1] == (
        f"sqlite_home={json.dumps(str(workdir.parent / 'state'))}"
    )
    assert argv[argv.index("-i") + 1] == str(image)
    assert runner.schema["additionalProperties"] is False
    assert runner.schema["required"] == [
        "summary",
        "steps",
        "success_criteria",
        "constraints",
        "artifact_content",
        "artifact_content_kind",
    ]
    assert "default" not in runner.schema["properties"]["constraints"]
    assert response.data["summary"] == "Codex plan"


@pytest.mark.asyncio
async def test_codex_exec_provider_preserves_cli_token_usage() -> None:
    structured = {
        "summary": "Codex plan",
        "steps": ["Inspect"],
        "success_criteria": ["Visible evidence"],
    }
    runner = CodexRecordingRunner(
        "\n".join(
            [
                json.dumps(
                    {"type": "thread.started", "thread_id": "thread-1"}
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": json.dumps(structured),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 80,
                            "output_tokens": 24,
                        },
                    }
                ),
            ]
        )
    )
    provider = CodexExecProvider(
        name="codex-account",
        model="gpt-5.6-sol",
        runner=runner,
        inherited_env=["PATH", "HOME"],
    )

    response = await provider.complete(request())

    assert "--json" in runner.calls[0]["argv"]
    assert response.data == structured
    assert response.usage == {
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 24,
    }


@pytest.mark.asyncio
async def test_codex_schema_normalizes_discriminated_action_union(
    tmp_path: Path,
) -> None:
    model_request = request().model_copy(
        update={
            "role": "controller",
            "output_schema": ControllerDecision.model_json_schema(),
        }
    )
    runner = CodexRecordingRunner(
        json.dumps(
            {
                "outcome": "done",
                "intent": "No action is needed.",
                "actions": [],
                "expected_evidence": [],
                "reason": "",
            }
        )
    )
    provider = CodexExecProvider(
        name="codex-oauth",
        runner=runner,
        inherited_env=["PATH", "HOME", "CODEX_HOME"],
    )

    await provider.complete(model_request)

    action_items = runner.schema["properties"]["actions"]["items"]
    assert "discriminator" not in action_items
    assert "oneOf" not in action_items
    assert len(action_items["anyOf"]) == 9


@pytest.mark.asyncio
async def test_codex_app_server_provider_uses_persistent_oauth_session(
    tmp_path: Path,
) -> None:
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"frame")
    session = CodexAppServerRecordingSession(
        {
            "summary": "Codex app-server plan",
            "steps": ["Inspect"],
            "success_criteria": ["Visible evidence"],
            "constraints": [],
        }
    )
    provider = CodexAppServerProvider(
        name="codex-app-server",
        model="gpt-5.6-luna",
        session=session,
        reasoning_effort="low",
        service_tier="priority",
    )

    response = await provider.complete(
        request().model_copy(update={"image_path": str(image)})
    )
    await provider.aclose()

    call = session.calls[0]
    schema = call["output_schema"]
    assert isinstance(schema, dict)
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "summary",
        "steps",
        "success_criteria",
        "constraints",
        "artifact_content",
        "artifact_content_kind",
    ]
    assert call["image_path"] == str(image)
    assert call["model"] == "gpt-5.6-luna"
    assert call["reasoning_effort"] == "low"
    assert call["service_tier"] == "priority"
    assert response.data["summary"] == "Codex app-server plan"
    assert response.usage["cached_input_tokens"] == 20
    assert response.latency_ms == 321
    assert session.closed is True


@pytest.mark.asyncio
async def test_claude_code_provider_uses_oauth_with_safe_read_only_image_access(
    tmp_path: Path,
) -> None:
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"frame")
    model_request = request().model_copy(update={"image_path": str(image)})
    runner = ClaudeRecordingRunner(
        json.dumps(
            {
                "subtype": "success",
                "structured_output": {
                    "summary": "Claude plan",
                    "steps": ["Inspect"],
                    "success_criteria": ["Visible evidence"],
                },
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        )
    )
    provider = ClaudeCodeProvider(
        name="claude-oauth",
        model="opus",
        runner=runner,
        inherited_env=["PATH", "HOME", "CLAUDE_CONFIG_DIR"],
        reasoning_effort="low",
    )

    response = await provider.complete(model_request)

    argv = runner.calls[0]["argv"]
    assert argv[:3] == ["claude", "-p", "--output-format"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "plan" not in argv
    assert argv[argv.index("--tools") + 1] == "Read"
    assert argv[argv.index("--allowedTools") + 1] == "Read"
    assert "--safe-mode" in argv
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "low"
    assert "--max-turns" not in argv
    assert runner.schema["required"] == [
        "summary",
        "steps",
        "success_criteria",
        "constraints",
        "artifact_content",
        "artifact_content_kind",
    ]
    assert runner.workspace_files == ["screen.jpg"]
    assert "screen.jpg" in runner.stdin
    assert response.data["summary"] == "Claude plan"
    assert response.usage == {"input_tokens": 10, "output_tokens": 20}


@pytest.mark.asyncio
async def test_claude_schema_normalizes_discriminated_action_union() -> None:
    model_request = request().model_copy(
        update={
            "role": "controller",
            "output_schema": ControllerDecision.model_json_schema(),
        }
    )
    runner = ClaudeRecordingRunner(
        json.dumps(
            {
                "subtype": "success",
                "structured_output": {
                    "outcome": "done",
                    "intent": "No action is needed.",
                    "actions": [],
                    "expected_evidence": [],
                    "reason": "",
                },
            }
        )
    )
    provider = ClaudeCodeProvider(
        name="claude-oauth",
        model="opus",
        runner=runner,
        inherited_env=["PATH", "HOME", "CLAUDE_CONFIG_DIR"],
    )

    await provider.complete(model_request)

    action_items = runner.schema["properties"]["actions"]["items"]
    assert "discriminator" not in action_items
    assert "oneOf" not in action_items
    assert len(action_items["anyOf"]) == 9
    assert runner.schema["required"] == [
        "outcome",
        "intent",
        "actions",
        "expected_evidence",
        "expects_task_completion",
        "reason",
    ]


@pytest.mark.asyncio
async def test_openai_compatible_provider_sends_schema_and_image_capable_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    seen: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(http_request.headers)
        seen["json"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "model": "fast-model",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "API plan",
                                    "steps": ["Inspect"],
                                    "success_criteria": ["Visible evidence"],
                                }
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        )

    provider = OpenAICompatibleProvider(
        name="gateway",
        model="fast-model",
        base_url="https://models.example/v1",
        api_key_env="TEST_MODEL_KEY",
        transport=httpx.MockTransport(handler),
    )

    response = await provider.complete(request())
    await provider.aclose()

    assert response.data["summary"] == "API plan"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert seen["json"]["response_format"]["type"] == "json_schema"
    assert seen["json"]["response_format"]["json_schema"]["schema"][
        "properties"
    ]["success_criteria"]
    assert (
        seen["json"]["response_format"]["json_schema"]["schema"][
            "additionalProperties"
        ]
        is False
    )
    assert response.usage == {"total_tokens": 42}


@pytest.mark.asyncio
async def test_native_openai_responses_provider_uses_structured_vision_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TEST_OPENAI_KEY", "secret")
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    model_request = request().model_copy(
        update={"image_path": str(screenshot)}
    )
    seen: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["path"] = http_request.url.path
        seen["headers"] = dict(http_request.headers)
        seen["json"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "status": "completed",
                "model": "gpt-test-snapshot",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "summary": "Responses plan",
                                        "steps": ["Inspect"],
                                        "success_criteria": [
                                            "Visible evidence"
                                        ],
                                    }
                                ),
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "total_tokens": 20,
                },
            },
        )

    provider = OpenAIResponsesProvider(
        name="openai-native",
        model="gpt-test",
        api_key_env="TEST_OPENAI_KEY",
        reasoning_effort="low",
        max_output_tokens=2048,
        transport=httpx.MockTransport(handler),
    )

    response = await provider.complete(model_request)
    await provider.aclose()

    assert seen["path"] == "/v1/responses"
    assert seen["headers"]["authorization"] == "Bearer secret"
    payload = seen["json"]
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_output_tokens"] == 2048
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert (
        payload["text"]["format"]["schema"]["additionalProperties"]
        is False
    )
    content = payload["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "Plan this task."}
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "high"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert response.model == "gpt-test-snapshot"
    assert response.data["summary"] == "Responses plan"
    assert response.usage["total_tokens"] == 20


@pytest.mark.asyncio
async def test_responses_provider_supports_an_api_key_header_auth_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AZURE_OPENAI_KEY", "azure-secret")
    seen: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["path"] = http_request.url.path
        seen["headers"] = dict(http_request.headers)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "model": "azure-deployment",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "summary": "Azure plan",
                                        "steps": ["Inspect"],
                                        "success_criteria": [
                                            "Visible evidence"
                                        ],
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    provider = OpenAIResponsesProvider(
        name="azure-openai",
        model="azure-deployment",
        base_url="https://resource.openai.azure.com/openai/v1",
        auth=EnvironmentHeaderAuth(
            env_name="TEST_AZURE_OPENAI_KEY",
            header="api-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await provider.complete(request())
    await provider.aclose()

    assert seen["path"] == "/openai/v1/responses"
    assert seen["headers"]["api-key"] == "azure-secret"
    assert "authorization" not in seen["headers"]
    assert response.data["summary"] == "Azure plan"


@pytest.mark.asyncio
async def test_responses_oauth_command_is_isolated_from_the_model_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("HOME", "/account/home")
    monkeypatch.setenv("PRIVATE_PROVIDER_KEY", "must-not-be-inherited")
    runner = RecordingRunner("entra-token\n")
    seen: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(http_request.headers)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "summary": "OAuth plan",
                                        "steps": ["Inspect"],
                                        "success_criteria": [
                                            "Visible evidence"
                                        ],
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    provider = OpenAIResponsesProvider(
        name="azure-entra",
        model="azure-deployment",
        base_url="https://resource.openai.azure.com/openai/v1",
        auth=CommandBearerAuth(
            name="azure-entra",
            argv=[
                "az",
                "account",
                "get-access-token",
                "--resource",
                "https://cognitiveservices.azure.com",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            runner=runner,
            inherited_env=["PATH", "HOME"],
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await provider.complete(request())
    await provider.aclose()

    call = runner.calls[0]
    assert call["stdin"] == ""
    assert call["env"] == {
        "PATH": "/safe/bin",
        "HOME": "/account/home",
    }
    assert "Plan this task." not in json.dumps(call)
    assert seen["headers"]["authorization"] == "Bearer entra-token"
    assert response.data["summary"] == "OAuth plan"


@pytest.mark.asyncio
async def test_oauth_command_rejects_multiline_or_oversized_credentials() -> None:
    multiline = CommandBearerAuth(
        name="unsafe-oauth",
        argv=["credential-helper"],
        runner=RecordingRunner("token\nunexpected"),
    )
    oversized = CommandBearerAuth(
        name="unsafe-oauth",
        argv=["credential-helper"],
        runner=RecordingRunner("x" * 16_385),
    )

    with pytest.raises(RuntimeError, match="invalid credential output"):
        await multiline.headers()
    with pytest.raises(RuntimeError, match="invalid credential output"):
        await oversized.headers()


@pytest.mark.asyncio
async def test_native_openai_responses_provider_fails_closed_on_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPENAI_KEY", "secret")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "refusal",
                                "refusal": "provider-controlled secret text",
                            }
                        ],
                    }
                ],
            },
        )

    provider = OpenAIResponsesProvider(
        name="openai-native",
        model="gpt-test",
        api_key_env="TEST_OPENAI_KEY",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="refused structured response") as exc:
        await provider.complete(request())
    await provider.aclose()

    assert "provider-controlled secret text" not in str(exc.value)


@pytest.mark.asyncio
async def test_api_provider_error_does_not_expose_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "do-not-leak-provider-body"},
        )

    provider = OpenAICompatibleProvider(
        name="gateway",
        model="fast-model",
        base_url="https://models.example/v1",
        api_key_env="TEST_MODEL_KEY",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.complete(request())
    await provider.aclose()

    message = str(caught.value)
    assert "401" in message
    assert "authentication-failed" in message
    assert "do-not-leak-provider-body" not in message


@pytest.mark.asyncio
async def test_api_transport_failure_is_redacted_at_shared_http_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")

    def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "private-route.example do-not-leak",
            request=http_request,
        )

    provider = OpenAICompatibleProvider(
        name="gateway",
        model="fast-model",
        base_url="https://models.example/v1",
        api_key_env="TEST_MODEL_KEY",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as caught:
        await provider.complete(request())
    await provider.aclose()

    message = str(caught.value)
    assert "transport" in message
    assert "provider-error" in message
    assert "private-route.example" not in message
    assert "do-not-leak" not in message


@pytest.mark.asyncio
async def test_anthropic_provider_uses_supported_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "anthropic-secret")
    seen: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(http_request.headers)
        seen["json"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "model": "claude-model",
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "summary": "Claude plan",
                                "steps": ["Inspect"],
                                "success_criteria": ["Visible evidence"],
                            }
                        ),
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    provider = AnthropicApiProvider(
        name="anthropic",
        model="claude-model",
        api_key_env="TEST_ANTHROPIC_KEY",
        transport=httpx.MockTransport(handler),
    )
    response = await provider.complete(request())
    await provider.aclose()

    assert seen["headers"]["x-api-key"] == "anthropic-secret"
    assert seen["json"]["output_config"]["format"] == {
        "type": "json_schema",
        "schema": request().output_schema,
    }
    assert response.data["summary"] == "Claude plan"


@pytest.mark.asyncio
async def test_gemini_provider_uses_api_key_header_and_response_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_GEMINI_KEY", "gemini-secret")
    seen: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["url"] = str(http_request.url)
        seen["headers"] = dict(http_request.headers)
        seen["json"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-model-001",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "summary": "Gemini plan",
                                            "steps": ["Inspect"],
                                            "success_criteria": ["Visible evidence"],
                                        }
                                    )
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {"totalTokenCount": 30},
            },
        )

    provider = GeminiApiProvider(
        name="gemini",
        model="gemini-model",
        api_key_env="TEST_GEMINI_KEY",
        transport=httpx.MockTransport(handler),
    )
    response = await provider.complete(request())
    await provider.aclose()

    assert seen["url"].endswith(
        "/v1beta/models/gemini-model:generateContent"
    )
    assert seen["headers"]["x-goog-api-key"] == "gemini-secret"
    assert seen["json"]["generationConfig"]["responseMimeType"] == (
        "application/json"
    )
    assert seen["json"]["generationConfig"]["responseJsonSchema"] == (
        request().output_schema
    )
    assert response.data["summary"] == "Gemini plan"


@pytest.mark.asyncio
async def test_gemini_provider_supports_vertex_bearer_auth_and_endpoint() -> None:
    runner = RecordingRunner("google-access-token\n")
    seen: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["url"] = str(http_request.url)
        seen["headers"] = dict(http_request.headers)
        seen["json"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-vertex-001",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "summary": "Vertex plan",
                                            "steps": ["Inspect"],
                                            "success_criteria": [
                                                "Visible evidence"
                                            ],
                                        }
                                    )
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {"totalTokenCount": 31},
            },
        )

    provider = GeminiApiProvider(
        name="vertex-gemini",
        model="gemini-model",
        base_url=(
            "https://aiplatform.googleapis.com/v1/projects/test-project/"
            "locations/global/publishers/google"
        ),
        auth=CommandBearerAuth(
            name="gcloud-auth",
            argv=["gcloud", "auth", "print-access-token"],
            runner=runner,
        ),
        transport=httpx.MockTransport(handler),
    )
    response = await provider.complete(request())
    await provider.aclose()

    assert seen["url"].endswith(
        "/v1/projects/test-project/locations/global/publishers/google/"
        "models/gemini-model:generateContent"
    )
    assert seen["headers"]["authorization"] == (
        "Bearer google-access-token"
    )
    assert "x-goog-api-key" not in seen["headers"]
    assert seen["json"]["generationConfig"]["responseJsonSchema"] == (
        request().output_schema
    )
    assert runner.calls[0]["stdin"] == ""
    assert response.data["summary"] == "Vertex plan"
