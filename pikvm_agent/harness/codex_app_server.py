"""Persistent, tool-disabled Codex app-server session for model provider calls."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class CodexAppServerTurnResult:
    """One completed structured app-server turn."""

    text: str
    model: str
    usage: dict[str, int]
    latency_ms: int


class CodexAppServerSession(Protocol):
    """Provider-facing app-server boundary used by tests and the live adapter."""

    async def complete(
        self,
        *,
        prompt: str,
        image_path: str | None,
        image_detail: str,
        output_schema: dict[str, Any],
        model: str,
        reasoning_effort: str,
        service_tier: str | None,
        timeout_s: float,
    ) -> CodexAppServerTurnResult: ...

    async def aclose(self) -> None: ...


@dataclass
class _TurnState:
    thread_id: str
    model: str
    future: asyncio.Future[CodexAppServerTurnResult]
    started: float
    turn_id: str | None = None
    answer: str | None = None
    final_answer: str | None = None
    usage: dict[str, int] | None = None


_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_tool",
    "unified_exec",
)
_FORBIDDEN_ITEM_TYPES = {
    "collabToolCall",
    "commandExecution",
    "dynamicToolCall",
    "fileChange",
    "imageView",
    "mcpToolCall",
    "webSearch",
}
_BASE_INSTRUCTIONS = (
    "You are a stateless computer-screen decision worker. Use only the user "
    "text and directly attached image. Do not call tools, browse, inspect the "
    "filesystem, run commands, change files, ask questions, or contact any "
    "external system. Return exactly one object matching the supplied output "
    "schema."
)


def _safe_failure_class(value: object) -> str:
    """Reduce provider-controlled diagnostics to a stable public class."""

    text = json.dumps(value, ensure_ascii=True).casefold()
    if any(marker in text for marker in ("unauthorized", "authentication")):
        return "authentication-failed"
    if "rate limit" in text or "ratelimit" in text:
        return "rate-limited"
    if any(marker in text for marker in ("quota", "billing", "usage limit")):
        return "quota-or-billing"
    if any(marker in text for marker in ("timeout", "timed out")):
        return "timeout"
    if any(
        marker in text
        for marker in (
            "connection failed",
            "disconnected",
            "overloaded",
            "server overloaded",
            "unavailable",
        )
    ):
        return "provider-unavailable"
    if "schema" in text or "structured" in text:
        return "structured-output-error"
    if any(marker in text for marker in ("bad request", "invalid request")):
        return "request-rejected"
    return "provider-error"


def _usage_from_notification(params: dict[str, Any]) -> dict[str, int] | None:
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return None
    last = token_usage.get("last")
    if not isinstance(last, dict):
        return None
    keys = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_tokens",
        "totalTokens": "total_tokens",
    }
    usage: dict[str, int] = {}
    for source, target in keys.items():
        value = last.get(source)
        if isinstance(value, int) and value >= 0:
            usage[target] = value
    return usage or None


class CodexAppServerClient:
    """Long-lived Codex JSON-RPC client using provider-owned ChatGPT auth."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        inherited_env: list[str] | None = None,
        startup_timeout_s: float = 20.0,
    ) -> None:
        self.executable = executable
        self.inherited_env = list(
            ["PATH", "HOME", "CODEX_HOME"]
            if inherited_env is None
            else inherited_env
        )
        self.startup_timeout_s = startup_timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._turns: dict[str, _TurnState] = {}
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_request_id = 1
        self._initialized = False
        self._closing = False
        self._closed = False
        self._stderr_tail = bytearray()

    def _argv(self) -> list[str]:
        argv = [self.executable, "app-server", "--stdio"]
        for feature in _DISABLED_FEATURES:
            argv.extend(["--disable", feature])
        argv.extend(
            [
                "-c",
                "mcp_servers={}",
                "-c",
                'web_search="disabled"',
                "-c",
                'approval_policy="never"',
                "-c",
                'sandbox_mode="read-only"',
            ]
        )
        return argv

    def _environment(self) -> dict[str, str]:
        env = {
            key: os.environ[key]
            for key in self.inherited_env
            if key in os.environ
        }
        env["NO_COLOR"] = "1"
        return env

    async def _ensure_started(self) -> None:
        if (
            self._initialized
            and self._process is not None
            and self._process.returncode is None
        ):
            return
        async with self._start_lock:
            if (
                self._initialized
                and self._process is not None
                and self._process.returncode is None
            ):
                return
            if self._closed:
                raise RuntimeError("codex app-server session is closed")
            await self._stop_process()
            self._closing = False
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._argv(),
                    env=self._environment(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                raise RuntimeError(
                    "codex app-server failed to start: executable-not-found"
                ) from exc
            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                process.kill()
                raise RuntimeError(
                    "codex app-server failed to start: provider-unavailable"
                )
            self._process = process
            self._reader_task = asyncio.create_task(
                self._read_loop(process),
                name="codex-app-server-reader",
            )
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(process),
                name="codex-app-server-stderr",
            )
            try:
                response = await self._request_started(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "pikvm_harness",
                            "title": "PiKVM Harness",
                            "version": "0.1.0",
                        }
                    },
                    timeout_s=self.startup_timeout_s,
                )
                if not isinstance(response.get("userAgent"), str):
                    raise RuntimeError(
                        "codex app-server initialization failed: "
                        "provider-unavailable"
                    )
                await self._notify_started("initialized", {})
            except Exception:
                await self._stop_process()
                raise
            self._initialized = True

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeError(
                "codex app-server is unavailable: provider-unavailable"
            )
        encoded = (
            json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        async with self._write_lock:
            process.stdin.write(encoded)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise RuntimeError(
                    "codex app-server disconnected: provider-unavailable"
                ) from exc

    async def _request_started(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            await self._write(
                {"method": method, "id": request_id, "params": params}
            )
            try:
                return await asyncio.wait_for(future, timeout=timeout_s)
            except TimeoutError as exc:
                raise RuntimeError(
                    "codex app-server request timed out: timeout"
                ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        await self._ensure_started()
        return await self._request_started(
            method,
            params,
            timeout_s=timeout_s,
        )

    async def _notify_started(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        await self._write({"method": method, "params": params})

    async def _read_loop(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        assert process.stdout is not None
        failure: RuntimeError | None = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    failure = RuntimeError(
                        "codex app-server returned invalid JSON: "
                        "provider-error"
                    )
                    break
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    await self._reject_server_request(message)
                elif "id" in message:
                    self._handle_response(message)
                elif "method" in message:
                    self._handle_notification(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = RuntimeError(
                "codex app-server stream failed: provider-unavailable"
            )
        finally:
            if not self._closing and process is self._process:
                if failure is None:
                    stderr = self._stderr_tail.decode(errors="replace")
                    failure = RuntimeError(
                        "codex app-server exited: "
                        f"{_safe_failure_class(stderr)}"
                    )
                self._fail_all(failure)
                self._initialized = False

    async def _drain_stderr(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        assert process.stderr is not None
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > 8192:
                    del self._stderr_tail[:-8192]
        except asyncio.CancelledError:
            raise

    async def _reject_server_request(
        self,
        message: dict[str, Any],
    ) -> None:
        await self._write(
            {
                "id": message["id"],
                "error": {
                    "code": -32000,
                    "message": "Provider-lane tools are disabled.",
                },
            }
        )

    def _handle_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        future = (
            self._pending.get(request_id)
            if isinstance(request_id, int)
            else None
        )
        if future is None or future.done():
            return
        error = message.get("error")
        if error is not None:
            future.set_exception(
                RuntimeError(
                    "codex app-server request failed: "
                    f"{_safe_failure_class(error)}"
                )
            )
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        state = self._turns.get(thread_id)
        if state is None or state.future.done():
            return
        turn_id = params.get("turnId")
        if (
            isinstance(turn_id, str)
            and state.turn_id is not None
            and turn_id != state.turn_id
        ):
            return
        if method == "thread/tokenUsage/updated":
            state.usage = _usage_from_notification(params)
            return
        if method == "item/completed":
            self._handle_item(state, params.get("item"))
            return
        if method == "item/started":
            self._reject_forbidden_item(state, params.get("item"))
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                state.future.set_exception(
                    RuntimeError(
                        "codex app-server returned no turn: provider-error"
                    )
                )
                return
            for item in turn.get("items") or []:
                self._handle_item(state, item)
            status = turn.get("status")
            if status != "completed":
                state.future.set_exception(
                    RuntimeError(
                        "codex app-server turn failed: "
                        f"{_safe_failure_class(turn.get('error') or status)}"
                    )
                )
                return
            answer = state.final_answer or state.answer
            if not answer:
                state.future.set_exception(
                    RuntimeError(
                        "codex app-server returned no structured agent "
                        "message: structured-output-error"
                    )
                )
                return
            state.future.set_result(
                CodexAppServerTurnResult(
                    text=answer,
                    model=state.model,
                    usage=state.usage or {},
                    latency_ms=round(
                        (time.monotonic() - state.started) * 1000
                    ),
                )
            )

    def _reject_forbidden_item(
        self,
        state: _TurnState,
        value: object,
    ) -> None:
        if not isinstance(value, dict):
            return
        item_type = value.get("type")
        if item_type in _FORBIDDEN_ITEM_TYPES and not state.future.done():
            state.future.set_exception(
                RuntimeError(
                    "codex app-server attempted a disabled tool: "
                    "request-rejected"
                )
            )

    def _handle_item(self, state: _TurnState, value: object) -> None:
        self._reject_forbidden_item(state, value)
        if state.future.done() or not isinstance(value, dict):
            return
        if value.get("type") != "agentMessage":
            return
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        if value.get("phase") == "final_answer":
            state.final_answer = text
        else:
            state.answer = text

    def _fail_all(self, failure: RuntimeError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(failure)
        for state in list(self._turns.values()):
            if not state.future.done():
                state.future.set_exception(failure)

    async def _best_effort_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(
                asyncio.shield(
                    self._request_started(
                        method,
                        params,
                        timeout_s=1.0,
                    )
                ),
                timeout=1.1,
            )

    async def complete(
        self,
        *,
        prompt: str,
        image_path: str | None,
        image_detail: str,
        output_schema: dict[str, Any],
        model: str,
        reasoning_effort: str,
        service_tier: str | None,
        timeout_s: float,
    ) -> CodexAppServerTurnResult:
        if image_detail not in {"auto", "low", "high", "original"}:
            raise ValueError(
                "Codex app-server image_detail must be auto, low, high, "
                "or original"
            )
        await self._ensure_started()
        started = time.monotonic()
        thread_id: str | None = None
        state: _TurnState | None = None
        try:
            async with asyncio.timeout(timeout_s):
                with tempfile.TemporaryDirectory(
                    prefix="pikvm-codex-app-server-"
                ) as tmp:
                    thread_params: dict[str, Any] = {
                        "modelProvider": "openai",
                        "cwd": str(Path(tmp)),
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "baseInstructions": _BASE_INSTRUCTIONS,
                        "developerInstructions": _BASE_INSTRUCTIONS,
                        "ephemeral": True,
                        "serviceName": "pikvm_harness",
                        "config": {
                            "web_search": "disabled",
                            "mcp_servers": {},
                        },
                    }
                    if model != "account-default":
                        thread_params["model"] = model
                    if service_tier is not None:
                        thread_params["serviceTier"] = service_tier
                    thread_response = await self._request(
                        "thread/start",
                        thread_params,
                        timeout_s=timeout_s,
                    )
                    thread = thread_response.get("thread")
                    if not isinstance(thread, dict) or not isinstance(
                        thread.get("id"), str
                    ):
                        raise RuntimeError(
                            "codex app-server returned no thread: "
                            "provider-error"
                        )
                    thread_id = thread["id"]
                    selected_model = str(
                        thread_response.get("model") or model
                    )
                    state = _TurnState(
                        thread_id=thread_id,
                        model=selected_model,
                        future=asyncio.get_running_loop().create_future(),
                        started=started,
                    )
                    self._turns[thread_id] = state
                    inputs: list[dict[str, Any]] = [
                        {"type": "text", "text": prompt}
                    ]
                    if image_path is not None:
                        image = Path(image_path)
                        if not image.is_file():
                            raise RuntimeError(
                                f"screenshot does not exist: {image}"
                            )
                        inputs.append(
                            {
                                "type": "localImage",
                                "path": str(image),
                                "detail": image_detail,
                            }
                        )
                    turn_params: dict[str, Any] = {
                        "threadId": thread_id,
                        "input": inputs,
                        "effort": reasoning_effort,
                        "summary": "none",
                        "outputSchema": output_schema,
                    }
                    if model != "account-default":
                        turn_params["model"] = model
                    if service_tier is not None:
                        turn_params["serviceTier"] = service_tier
                    turn_response = await self._request(
                        "turn/start",
                        turn_params,
                        timeout_s=timeout_s,
                    )
                    turn = turn_response.get("turn")
                    if not isinstance(turn, dict) or not isinstance(
                        turn.get("id"), str
                    ):
                        raise RuntimeError(
                            "codex app-server returned no turn: "
                            "provider-error"
                        )
                    state.turn_id = turn["id"]
                    return await state.future
        except TimeoutError as exc:
            if thread_id is not None and state is not None:
                await self._best_effort_request(
                    "turn/interrupt",
                    {
                        "threadId": thread_id,
                        "turnId": state.turn_id,
                    },
                )
            raise RuntimeError(
                "codex app-server turn timed out: timeout"
            ) from exc
        except asyncio.CancelledError:
            if thread_id is not None and state is not None:
                await self._best_effort_request(
                    "turn/interrupt",
                    {
                        "threadId": thread_id,
                        "turnId": state.turn_id,
                    },
                )
            raise
        finally:
            if thread_id is not None:
                self._turns.pop(thread_id, None)
                await self._best_effort_request(
                    "thread/unsubscribe",
                    {"threadId": thread_id},
                )

    async def _stop_process(self) -> None:
        process = self._process
        self._closing = True
        self._initialized = False
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=2.0)
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_all(
            RuntimeError("codex app-server stopped: provider-unavailable")
        )
        self._pending.clear()
        self._turns.clear()
        self._process = None
        self._reader_task = None
        self._stderr_task = None
        self._stderr_tail.clear()
        self._closing = False

    async def aclose(self) -> None:
        self._closed = True
        await self._stop_process()
