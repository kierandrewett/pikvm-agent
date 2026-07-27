"""Model-provider adapters for API keys, gateways, and existing CLI logins.

The harness never reads or copies a CLI's OAuth tokens.  A subprocess adapter
invokes the provider's supported headless command and lets that command own its
credential store.  API adapters read one explicitly named environment variable.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import re
import signal
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from pikvm_agent.harness.agent_models import ModelRequest, ModelResponse


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise RuntimeError(f"expected JSON object, got {type(value).__name__}")
    text = value.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        # Some provider CLIs include a short prefix despite JSON instructions.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"provider did not return JSON: {exc}") from exc
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested:
            raise RuntimeError(f"provider did not return valid JSON: {nested}") from nested
    if not isinstance(parsed, dict):
        raise RuntimeError("provider JSON response must be an object")
    return parsed


def _codex_jsonl_result(value: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract the final structured answer and usage from ``codex exec --json``."""

    text = value.strip()
    try:
        direct = json.loads(text)
    except json.JSONDecodeError:
        direct = None
    if isinstance(direct, dict) and "type" not in direct:
        # Keeps deterministic provider fakes and older captured fixtures useful.
        return direct, {}

    answer: dict[str, Any] | None = None
    usage: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("codex CLI returned invalid JSONL") from exc
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                answer = _json_object(item["text"])
        elif event.get("type") == "turn.completed":
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usage = candidate
        elif event.get("type") in {"error", "turn.failed"}:
            raise RuntimeError("codex CLI structured run failed")
    if answer is None:
        raise RuntimeError("codex CLI returned no structured agent message")
    return answer, usage


def _select_path(value: Any, dotted_path: str) -> Any:
    current = value
    if not dotted_path:
        return current
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise RuntimeError(f"provider response has no {dotted_path!r}")
        current = current[part]
    return current


def _safe_failure_class(text: str = "", status: object = None) -> str:
    """Classify a provider failure without returning provider-controlled text."""

    try:
        code = int(status) if status not in (None, "", "?") else None
    except (TypeError, ValueError):
        code = None
    lower = text.casefold()
    if code in {401, 403} or any(
        marker in lower
        for marker in ("invalid api key", "unauthorized", "authentication failed")
    ):
        return "authentication-failed"
    if code == 429 or "rate limit" in lower:
        return "rate-limited"
    if code == 402 or any(
        marker in lower for marker in ("quota", "billing", "credit balance")
    ):
        return "quota-or-billing"
    if code in {408, 504} or "timed out" in lower or "timeout" in lower:
        return "timeout"
    if code in {500, 502, 503, 529} or "overloaded" in lower:
        return "provider-unavailable"
    if "schema" in lower or "structured_output" in lower:
        return "structured-output-error"
    if code is not None and 400 <= code < 500:
        return "request-rejected"
    if code is not None and code >= 500:
        return "provider-unavailable"
    return "provider-error"


def _process_failure(name: str, result: ProcessResult) -> RuntimeError:
    """Build a useful CLI failure without dumping provider-controlled output."""

    payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            payload = _json_object(result.stdout)
        except RuntimeError:
            payload = {}
    parts = []
    for key in ("terminal_reason", "subtype"):
        value = str(payload.get(key) or "")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            parts.append(f"{key}={value}")
    status = payload.get("api_error_status")
    if isinstance(status, int) and 100 <= status <= 599:
        parts.append(f"api_error_status={status}")
    diagnostic_text = " ".join(
        (
            result.stderr[-2000:],
            str(payload.get("result") or "")[-2000:],
        )
    )
    parts.append(f"class={_safe_failure_class(diagnostic_text, status)}")
    return RuntimeError(
        f"{name} exited {result.returncode}: {', '.join(parts)}"
    )


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    async def run(
        self,
        *,
        argv: list[str],
        stdin: str,
        cwd: str | None,
        timeout_s: float,
        env: dict[str, str],
    ) -> ProcessResult: ...


class RequestAuth(Protocol):
    """Return request headers without exposing credentials to the model lane."""

    async def headers(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class EnvironmentHeaderAuth:
    """Read one named secret at call time and place it in one auth header."""

    env_name: str
    header: str = "Authorization"
    scheme: str = ""

    def __post_init__(self) -> None:
        allowed_headers = {
            "authorization",
            "api-key",
            "x-api-key",
            "x-goog-api-key",
        }
        if not self.env_name:
            raise ValueError("credential environment name is required")
        if self.header.casefold() not in allowed_headers:
            raise ValueError("unsupported credential header")

    async def headers(self) -> dict[str, str]:
        credential = os.environ.get(self.env_name)
        if not credential:
            raise RuntimeError(f"{self.env_name} is not set")
        return {self.header: f"{self.scheme}{credential}"}


class AsyncioProcessRunner:
    """No-shell subprocess runner so prompts cannot become command syntax."""

    @staticmethod
    async def _stop_process(process: Any, communicate_task: asyncio.Task[Any]) -> None:
        """Kill the provider process tree, then reap without an unbounded wait."""
        killed_group = False
        pid = getattr(process, "pid", None)
        if os.name != "nt" and isinstance(pid, int):
            try:
                os.killpg(pid, signal.SIGKILL)
                killed_group = True
            except ProcessLookupError:
                killed_group = True
            except OSError:
                pass
        if not killed_group and getattr(process, "returncode", None) is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

        # Killing first is important: a model CLI may leave descendants holding
        # stdout/stderr open, so cancelling communicate() before the process group
        # exits can itself wait forever.
        if not communicate_task.done():
            communicate_task.cancel()
        await asyncio.wait({communicate_task}, timeout=0.5)

        wait_task = asyncio.create_task(process.wait())
        done, _ = await asyncio.wait({wait_task}, timeout=2.0)
        if not done:
            wait_task.cancel()

    async def run(
        self,
        *,
        argv: list[str],
        stdin: str,
        cwd: str | None,
        timeout_s: float,
        env: dict[str, str],
    ) -> ProcessResult:
        if not argv:
            raise RuntimeError("provider command is empty")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        communicate_task = asyncio.create_task(process.communicate(stdin.encode()))
        try:
            done, _ = await asyncio.wait({communicate_task}, timeout=timeout_s)
            if not done:
                await self._stop_process(process, communicate_task)
                raise RuntimeError(
                    f"provider command timed out after {timeout_s:.1f}s"
                )
            stdout, stderr = communicate_task.result()
        except asyncio.CancelledError:
            await self._stop_process(process, communicate_task)
            raise
        return ProcessResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )


class CommandBearerAuth:
    """Acquire a short-lived bearer credential through an exact argv vector.

    The command receives no model prompt, schema, screenshot, or shell. Only
    explicitly allow-listed environment variables are inherited.
    """

    def __init__(
        self,
        *,
        name: str,
        argv: list[str],
        runner: ProcessRunner | None = None,
        inherited_env: list[str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if not name or not argv:
            raise ValueError("credential source name and argv are required")
        self.name = name
        self.argv = list(argv)
        self.runner = runner or AsyncioProcessRunner()
        self.inherited_env = list(inherited_env or ["PATH"])
        self.timeout_s = timeout_s

    async def headers(self) -> dict[str, str]:
        env = {
            key: os.environ[key]
            for key in self.inherited_env
            if key in os.environ
        }
        result = await self.runner.run(
            argv=self.argv,
            stdin="",
            cwd=None,
            timeout_s=self.timeout_s,
            env=env,
        )
        if result.returncode != 0:
            raise _process_failure(self.name, result)
        credential = result.stdout.strip()
        if (
            not credential
            or len(credential) > 16_384
            or any(character.isspace() for character in credential)
        ):
            raise RuntimeError(
                f"{self.name} returned invalid credential output"
            )
        return {"Authorization": f"Bearer {credential}"}


class SubprocessJsonProvider:
    """Generic adapter for any supported headless CLI or local model bridge.

    ``argv`` is an exact argument vector, never a shell string.  The model prompt
    and JSON Schema are sent over stdin, avoiding command-line length limits and
    accidental shell expansion.  ``response_path`` selects a nested field used
    by wrapper formats such as Gemini CLI's ``response`` property.
    """

    def __init__(
        self,
        *,
        name: str,
        model: str,
        argv: list[str],
        response_path: str = "",
        runner: ProcessRunner | None = None,
        inherited_env: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        if not name or not model or not argv:
            raise ValueError("name, model, and argv are required")
        self.name = name
        self.model = model
        self.argv = list(argv)
        self.response_path = response_path
        self.runner = runner or AsyncioProcessRunner()
        self.inherited_env = list(inherited_env or ["PATH"])
        self.extra_env = dict(extra_env or {})
        self.cwd = cwd
        self.timeout_s = timeout_s

    async def complete(self, request: ModelRequest) -> ModelResponse:
        stdin = (
            f"{request.prompt}\n\nOUTPUT JSON SCHEMA:\n"
            f"{json.dumps(request.output_schema, ensure_ascii=False, sort_keys=True)}"
        )
        if request.image_path:
            stdin += (
                "\n\nSCREENSHOT PATH (read-only evidence; do not modify it):\n"
                f"{request.image_path}"
            )
        env = {
            key: os.environ[key]
            for key in self.inherited_env
            if key in os.environ
        }
        env.update(self.extra_env)
        started = time.monotonic()
        result = await self.runner.run(
            argv=self.argv,
            stdin=stdin,
            cwd=self.cwd,
            timeout_s=self.timeout_s,
            env=env,
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            raise _process_failure(self.name, result)
        outer = _json_object(result.stdout)
        selected = _select_path(outer, self.response_path)
        data = _json_object(selected)
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data=data,
            latency_ms=latency_ms,
        )


class CodexExecProvider:
    """Supported ``codex exec`` bridge for cached ChatGPT/API authentication.

    The run is ephemeral, isolated in an empty temporary workspace, read-only,
    and ignores user config/rules so it cannot inherit unrelated MCP tools.
    Codex's SQLite runtime state lives in a separate writable sibling directory;
    the CLI keeps ownership of authentication under ``CODEX_HOME``.  The
    screenshot is attached with Codex's native ``-i`` flag and the final answer
    is constrained by ``--output-schema``.
    """

    def __init__(
        self,
        *,
        name: str,
        model: str = "account-default",
        executable: str = "codex",
        runner: ProcessRunner | None = None,
        inherited_env: list[str] | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.name = name
        self.model = model
        self.executable = executable
        self.runner = runner or AsyncioProcessRunner()
        self.inherited_env = list(
            inherited_env or ["PATH", "HOME", "CODEX_HOME"]
        )
        self.timeout_s = timeout_s

    async def complete(self, request: ModelRequest) -> ModelResponse:
        env = {
            key: os.environ[key]
            for key in self.inherited_env
            if key in os.environ
        }
        with tempfile.TemporaryDirectory(prefix="pikvm-codex-provider-") as tmp:
            root = Path(tmp)
            workdir = root / "workspace"
            state_dir = root / "state"
            workdir.mkdir()
            state_dir.mkdir()
            schema_path = root / "output.schema.json"
            schema_path.write_text(
                json.dumps(
                    _strict_json_schema(request.output_schema),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            argv = [
                self.executable,
                "exec",
                "--json",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "-c",
                f"sqlite_home={json.dumps(str(state_dir))}",
                "--output-schema",
                str(schema_path),
            ]
            if self.model != "account-default":
                argv.extend(["--model", self.model])
            if request.image_path:
                image = Path(request.image_path)
                if not image.is_file():
                    raise RuntimeError(f"screenshot does not exist: {image}")
                argv.extend(["-i", str(image)])
            argv.append("-")
            started = time.monotonic()
            result = await self.runner.run(
                argv=argv,
                stdin=request.prompt,
                cwd=str(workdir),
                timeout_s=self.timeout_s,
                env=env,
            )
            latency_ms = round((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            raise _process_failure(self.name, result)
        data, usage = _codex_jsonl_result(result.stdout)
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data=data,
            usage=usage,
            latency_ms=latency_ms,
        )


class ClaudeCodeProvider:
    """Supported Claude Code print-mode bridge for cached account OAuth.

    The provider runs in an empty temporary workspace with safe mode enabled,
    no MCP servers, no session persistence, and at most the built-in Read tool.
    When a screenshot is present it is copied into that workspace as the only
    readable artifact.  Claude Code owns its credential store; the harness
    neither locates nor reads the OAuth token.
    """

    def __init__(
        self,
        *,
        name: str,
        model: str = "account-default",
        executable: str = "claude",
        runner: ProcessRunner | None = None,
        inherited_env: list[str] | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.name = name
        self.model = model
        self.executable = executable
        self.runner = runner or AsyncioProcessRunner()
        self.inherited_env = list(
            inherited_env or ["PATH", "HOME", "CLAUDE_CONFIG_DIR"]
        )
        self.timeout_s = timeout_s

    async def complete(self, request: ModelRequest) -> ModelResponse:
        env = {
            key: os.environ[key]
            for key in self.inherited_env
            if key in os.environ
        }
        schema = _claude_json_schema(request.output_schema)
        with tempfile.TemporaryDirectory(prefix="pikvm-claude-provider-") as tmp:
            workdir = Path(tmp)
            prompt = request.prompt
            has_image = bool(request.image_path)
            if request.image_path:
                image = Path(request.image_path)
                if not image.is_file():
                    raise RuntimeError(f"screenshot does not exist: {image}")
                evidence_name = f"screen{image.suffix.lower() or '.jpg'}"
                evidence = workdir / evidence_name
                shutil.copyfile(image, evidence)
                prompt += (
                    "\n\nThe only workspace artifact is the current screen "
                    f"evidence at {evidence_name}. Use the Read tool to inspect "
                    "it before deciding. Do not infer pixels from the filename."
                )
            argv = [
                self.executable,
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema, sort_keys=True, separators=(",", ":")),
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--safe-mode",
                "--disable-slash-commands",
                "--strict-mcp-config",
                "--tools",
                "Read" if has_image else "",
            ]
            if has_image:
                argv.extend(["--allowedTools", "Read"])
            if self.model != "account-default":
                argv.extend(["--model", self.model])
            started = time.monotonic()
            result = await self.runner.run(
                argv=argv,
                stdin=prompt,
                cwd=str(workdir),
                timeout_s=self.timeout_s,
                env=env,
            )
            latency_ms = round((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            raise _process_failure(self.name, result)
        outer = _json_object(result.stdout)
        if outer.get("is_error") or (
            outer.get("subtype") and outer.get("subtype") != "success"
        ):
            diagnostic = str(
                outer.get("errors") or outer.get("result") or ""
            )
            raise RuntimeError(
                f"{self.name} structured run failed: "
                f"{_safe_failure_class(diagnostic)}"
            )
        data = outer.get("structured_output")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"{self.name} returned no validated structured_output"
            )
        return ModelResponse(
            provider=self.name,
            model=str(outer.get("model") or self.model),
            data=data,
            usage=outer.get("usage") or {},
            latency_ms=latency_ms,
        )


class GeminiCliProvider:
    """Gemini CLI bridge for an isolated, CLI-owned Google login.

    Gemini CLI keeps authentication and configuration under the same home.
    Reusing a normal interactive profile would therefore also inherit its MCP
    servers, extensions, hooks, skills, and context.  This adapter requires a
    dedicated profile path supplied through one named environment variable.
    It then overlays harness-owned system settings, an explicit extension
    selection, and an admin policy for every invocation:

    * the MCP catalogue/allow-list are empty;
    * skills, hooks, and ambient context are disabled;
    * ``--extensions none`` excludes extensions at launch;
    * every model tool is denied by the supplemental admin policy;
    * the working directory contains only the copied screen evidence;
    * the OAuth token remains owned and read exclusively by Gemini CLI.

    Gemini CLI 0.35.3 deliberately replaces file-based ``admin.*`` settings
    while merging effective settings, so this adapter does not rely on those
    fields. The normal settings above, launch flag, and policy are separate
    fail-closed layers.

    Gemini CLI 0.35 exposes JSON framing but not a response-schema flag, so the
    model is instructed to return the requested schema and the harness performs
    the authoritative Pydantic validation before any computer action.
    """

    _SYSTEM_SETTINGS = {
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
    _DENY_ALL_POLICY = (
        '[[rule]]\n'
        'toolName = "*"\n'
        'decision = "deny"\n'
        "priority = 999\n"
        'deny_message = "Model tools are disabled in the PiKVM harness provider lane."\n'
    )
    _DEFAULT_ENV = [
        "PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    ]
    _CREDENTIAL_ENV_NAMES = {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    }

    def __init__(
        self,
        *,
        name: str,
        model: str = "account-default",
        profile_home_env: str,
        executable: str = "gemini",
        runner: ProcessRunner | None = None,
        inherited_env: list[str] | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        if not name or not profile_home_env:
            raise ValueError("name and profile_home_env are required")
        selected_env = list(inherited_env or self._DEFAULT_ENV)
        forbidden = self._CREDENTIAL_ENV_NAMES.intersection(selected_env)
        if forbidden:
            raise ValueError(
                "Gemini CLI must use its dedicated saved login, not API-key "
                "environment variables"
            )
        self.name = name
        self.model = model
        self.profile_home_env = profile_home_env
        self.executable = executable
        self.runner = runner or AsyncioProcessRunner()
        self.inherited_env = selected_env
        self.timeout_s = timeout_s

    def _profile_home(self) -> Path:
        value = os.environ.get(self.profile_home_env)
        if not value:
            raise RuntimeError(
                f"{self.name} dedicated profile environment is not configured"
            )
        profile = Path(value)
        if not profile.is_absolute() or not profile.is_dir():
            raise RuntimeError(
                f"{self.name} dedicated profile directory is unavailable"
            )
        if profile.resolve() == Path.home().resolve():
            raise RuntimeError(
                f"{self.name} dedicated profile must not reuse the normal user home"
            )
        return profile

    async def complete(self, request: ModelRequest) -> ModelResponse:
        profile_home = self._profile_home()
        inherited = {
            key: os.environ[key]
            for key in self.inherited_env
            if key in os.environ
        }
        with tempfile.TemporaryDirectory(
            prefix="pikvm-gemini-provider-"
        ) as tmp:
            root = Path(tmp)
            workdir = root / "workspace"
            control = root / "control"
            workdir.mkdir()
            control.mkdir()
            system_settings = control / "system-settings.json"
            system_defaults = control / "system-defaults.json"
            policy = control / "deny-all-tools.toml"
            system_settings.write_text(
                json.dumps(self._SYSTEM_SETTINGS, sort_keys=True),
                encoding="utf-8",
            )
            system_defaults.write_text("{}", encoding="utf-8")
            policy.write_text(self._DENY_ALL_POLICY, encoding="utf-8")

            prompt = (
                f"{request.prompt}\n\n"
                "OUTPUT JSON SCHEMA:\n"
                f"{json.dumps(request.output_schema, ensure_ascii=False, sort_keys=True)}"
                "\n\nReturn exactly one JSON object and no Markdown fence."
            )
            if request.image_path:
                image = Path(request.image_path)
                if not image.is_file():
                    raise RuntimeError(f"screenshot does not exist: {image}")
                suffix = image.suffix.lower()
                if suffix not in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
                    suffix = ".png"
                evidence = workdir / f"screen{suffix}"
                shutil.copyfile(image, evidence)
                prompt += (
                    "\n\nCURRENT SCREEN EVIDENCE (inspect this image):\n"
                    f"@{evidence.name}"
                )

            env = {
                **inherited,
                "GEMINI_CLI_HOME": str(profile_home),
                "GEMINI_CLI_SURFACE": "pikvm-harness",
                "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(system_settings),
                "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(system_defaults),
                "NO_COLOR": "1",
            }
            argv = [
                self.executable,
                "--prompt",
                "",
                "--output-format",
                "json",
                "--approval-mode",
                "plan",
                "--extensions",
                "none",
                "--admin-policy",
                str(policy),
            ]
            if self.model != "account-default":
                argv.extend(["--model", self.model])
            started = time.monotonic()
            result = await self.runner.run(
                argv=argv,
                stdin=prompt,
                cwd=str(workdir),
                timeout_s=self.timeout_s,
                env=env,
            )
            latency_ms = round((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            raise _process_failure(self.name, result)
        outer = _json_object(result.stdout)
        if outer.get("error"):
            failure = _safe_failure_class(
                json.dumps(outer["error"], ensure_ascii=False)
            )
            raise RuntimeError(
                f"{self.name} structured run failed: {failure}"
            )
        response_text = outer.get("response")
        if not isinstance(response_text, str):
            raise RuntimeError(
                f"{self.name} returned no structured response"
            )
        data = _json_object(response_text)
        usage = outer.get("stats")
        return ModelResponse(
            provider=self.name,
            model=self.model,
            data=data,
            usage=usage if isinstance(usage, dict) else {},
            latency_ms=latency_ms,
        )


def _draft7_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate Pydantic's local ``$defs`` references for Claude SDK draft-07."""

    def convert(value: Any) -> Any:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            if isinstance(value, str) and value.startswith("#/$defs/"):
                return value.replace("#/$defs/", "#/definitions/", 1)
            return value
        converted: dict[str, Any] = {}
        for key, item in value.items():
            target = "definitions" if key == "$defs" else key
            converted[target] = convert(item)
        return converted

    return convert(schema)


def _claude_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply Claude's strict structured-output subset, then draft-07 refs."""

    return _draft7_schema(_strict_json_schema(schema))


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize Pydantic output for strict OpenAI/Codex response schemas."""

    def convert(value: Any) -> Any:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"default", "discriminator"}:
                continue
            target = "anyOf" if key == "oneOf" else key
            converted[target] = convert(item)
        properties = converted.get("properties")
        if isinstance(properties, dict):
            converted["additionalProperties"] = False
            converted["required"] = list(properties)
        return converted

    return convert(schema)


class _HttpApiProvider:
    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key_env: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 90.0,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _api_key(self) -> str:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set")
        return api_key

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                transport=self._transport, timeout=self.timeout_s
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def _post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> tuple[httpx.Response, int]:
        started = time.monotonic()
        try:
            response = await self._client().post(
                url,
                headers=headers,
                json=payload,
            )
        except httpx.RequestError as exc:
            failure_class = _safe_failure_class(str(exc))
            raise RuntimeError(
                f"{self.name} transport failed: {failure_class}"
            ) from exc
        return response, round((time.monotonic() - started) * 1000)


class OpenAICompatibleProvider(_HttpApiProvider):
    """Structured chat-completions adapter for OpenAI-compatible gateways."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key_env: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 90.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            transport=transport,
            timeout_s=timeout_s,
        )
        self.extra_headers = dict(headers or {})

    def _content(self, request: ModelRequest) -> str | list[dict[str, Any]]:
        if not request.image_path:
            return request.prompt
        path = Path(request.image_path)
        if not path.is_file():
            raise RuntimeError(f"screenshot does not exist: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode()
        return [
            {"type": "text", "text": request.prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            },
        ]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        api_key = self._api_key()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": self._content(request)}],
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": f"pikvm_{request.role}",
                    "strict": True,
                    "schema": _strict_json_schema(request.output_schema),
                },
            },
        }
        response, latency_ms = await self._post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            payload=payload,
        )
        try:
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            failure_class = _safe_failure_class(
                response.text,
                response.status_code,
            )
            raise RuntimeError(
                f"{self.name} request failed "
                f"({response.status_code}): {failure_class}"
            ) from exc
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        return ModelResponse(
            provider=self.name,
            model=str(body.get("model") or self.model),
            data=_json_object(content),
            usage=body.get("usage") or {},
            latency_ms=latency_ms,
        )


class OpenAIResponsesProvider(_HttpApiProvider):
    """Native OpenAI Responses API adapter with structured vision output."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key_env: str | None = None,
        auth: RequestAuth | None = None,
        base_url: str = "https://api.openai.com/v1",
        reasoning_effort: str | None = None,
        max_output_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 90.0,
    ) -> None:
        super().__init__(
            name=name,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env or "",
            transport=transport,
            timeout_s=timeout_s,
        )
        if auth is not None and api_key_env is not None:
            raise ValueError("provide auth or api_key_env, not both")
        if auth is None:
            if api_key_env is None:
                raise ValueError("auth or api_key_env is required")
            auth = EnvironmentHeaderAuth(
                env_name=api_key_env,
                scheme="Bearer ",
            )
        self.auth = auth
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens

    def _input_content(
        self, request: ModelRequest
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": request.prompt}
        ]
        if request.image_path:
            path = Path(request.image_path)
            if not path.is_file():
                raise RuntimeError(f"screenshot does not exist: {path}")
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode()
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{encoded}",
                    "detail": "high",
                }
            )
        return content

    @staticmethod
    def _output_text(body: dict[str, Any]) -> str:
        text_parts: list[str] = []
        refused = False
        for item in body.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    refused = True
                elif (
                    part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    text_parts.append(part["text"])
        if refused:
            raise RuntimeError("provider refused structured response")
        text = "".join(text_parts)
        if not text:
            raise RuntimeError("provider returned no structured output text")
        return text

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": self._input_content(request),
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": f"pikvm_{request.role}",
                    "strict": True,
                    "schema": _strict_json_schema(request.output_schema),
                }
            },
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        if self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        headers = {
            **await self.auth.headers(),
            "Content-Type": "application/json",
        }
        response, latency_ms = await self._post(
            f"{self.base_url}/responses",
            headers=headers,
            payload=payload,
        )
        try:
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            failure_class = _safe_failure_class(
                response.text,
                response.status_code,
            )
            raise RuntimeError(
                f"{self.name} request failed "
                f"({response.status_code}): {failure_class}"
            ) from exc
        if body.get("status") != "completed":
            raise RuntimeError(
                f"{self.name} returned incomplete structured response"
            )
        return ModelResponse(
            provider=self.name,
            model=str(body.get("model") or self.model),
            data=_json_object(self._output_text(body)),
            usage=body.get("usage") or {},
            latency_ms=latency_ms,
        )


class AnthropicApiProvider(_HttpApiProvider):
    """Official Anthropic Messages API adapter with JSON-schema output."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key_env: str,
        base_url: str = "https://api.anthropic.com/v1",
        max_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 90.0,
    ) -> None:
        super().__init__(
            name=name,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            transport=transport,
            timeout_s=timeout_s,
        )
        self.max_tokens = max_tokens

    def _content(self, request: ModelRequest) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if request.image_path:
            path = Path(request.image_path)
            if not path.is_file():
                raise RuntimeError(f"screenshot does not exist: {path}")
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(path.read_bytes()).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": request.prompt})
        return content

    async def complete(self, request: ModelRequest) -> ModelResponse:
        api_key = self._api_key()
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": self._content(request)}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": request.output_schema,
                }
            },
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            response, latency_ms = await self._post(
                f"{self.base_url}/messages",
                headers=headers,
                payload=payload,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", "?")
            diagnostic = (
                getattr(getattr(exc, "response", None), "text", "")
                or str(exc)
            )
            raise RuntimeError(
                f"{self.name} request failed "
                f"({status}): {_safe_failure_class(diagnostic, status)}"
            ) from exc
        stop_reason = body.get("stop_reason")
        if stop_reason in {"refusal", "max_tokens"}:
            raise RuntimeError(f"{self.name} stopped with {stop_reason}")
        text = "".join(
            item.get("text", "")
            for item in body.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        )
        return ModelResponse(
            provider=self.name,
            model=str(body.get("model") or self.model),
            data=_json_object(text),
            usage=body.get("usage") or {},
            latency_ms=latency_ms,
        )


class GeminiApiProvider(_HttpApiProvider):
    """Official Gemini generateContent adapter with controlled JSON output."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key_env: str | None = None,
        auth: RequestAuth | None = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 90.0,
    ) -> None:
        super().__init__(
            name=name,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env or "",
            transport=transport,
            timeout_s=timeout_s,
        )
        if auth is not None and api_key_env is not None:
            raise ValueError("provide auth or api_key_env, not both")
        if auth is None:
            if api_key_env is None:
                raise ValueError("auth or api_key_env is required")
            auth = EnvironmentHeaderAuth(
                env_name=api_key_env,
                header="x-goog-api-key",
            )
        self.auth = auth

    def _parts(self, request: ModelRequest) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [{"text": request.prompt}]
        if request.image_path:
            path = Path(request.image_path)
            if not path.is_file():
                raise RuntimeError(f"screenshot does not exist: {path}")
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime,
                        "data": base64.b64encode(path.read_bytes()).decode(),
                    }
                }
            )
        return parts

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "contents": [{"role": "user", "parts": self._parts(request)}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": request.output_schema,
            },
        }
        headers = {
            **await self.auth.headers(),
            "content-type": "application/json",
        }
        try:
            response, latency_ms = await self._post(
                f"{self.base_url}/models/{self.model}:generateContent",
                headers=headers,
                payload=payload,
            )
            response.raise_for_status()
            body = response.json()
            candidate = body["candidates"][0]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", "?")
            diagnostic = (
                getattr(getattr(exc, "response", None), "text", "")
                or str(exc)
            )
            raise RuntimeError(
                f"{self.name} request failed "
                f"({status}): {_safe_failure_class(diagnostic, status)}"
            ) from exc
        finish_reason = str(candidate.get("finishReason") or "")
        if finish_reason not in {"", "STOP"}:
            raise RuntimeError(
                f"{self.name} stopped with {finish_reason}"
            )
        text = "".join(
            part.get("text", "")
            for part in candidate.get("content", {}).get("parts", [])
            if isinstance(part, dict)
        )
        return ModelResponse(
            provider=self.name,
            model=str(body.get("modelVersion") or self.model),
            data=_json_object(text),
            usage=body.get("usageMetadata") or {},
            latency_ms=latency_ms,
        )
