from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pikvm_agent.harness.codex_app_server import CodexAppServerClient


class FakeStdout:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()


class FakeStderr:
    async def read(self, _size: int) -> bytes:
        return b""


class FakeStdin:
    def __init__(self, process: "FakeAppServerProcess") -> None:
        self.process = process

    def write(self, value: bytes) -> None:
        for line in value.splitlines():
            if line:
                self.process.receive(json.loads(line))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class FakeAppServerProcess:
    def __init__(self) -> None:
        self.stdout = FakeStdout()
        self.stderr = FakeStderr()
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.thread_counter = 0
        self.pending_thread_ids: list[str] = []

    def emit(self, message: dict[str, Any]) -> None:
        self.stdout.lines.put_nowait(
            (
                json.dumps(message, separators=(",", ":"))
                + "\n"
            ).encode()
        )

    def receive(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self.emit(
                {
                    "id": request_id,
                    "result": {"userAgent": "Codex test app-server"},
                }
            )
        elif method == "thread/start":
            self.thread_counter += 1
            thread_id = f"thread-{self.thread_counter}"
            self.pending_thread_ids.append(thread_id)
            self.emit(
                {
                    "id": request_id,
                    "result": {
                        "thread": {"id": thread_id},
                        "model": "gpt-5.6-luna",
                    },
                }
            )
        elif method == "turn/start":
            thread_id = str(message["params"]["threadId"])
            turn_id = f"turn-{thread_id}"
            self.emit(
                {
                    "id": request_id,
                    "result": {
                        "turn": {
                            "id": turn_id,
                            "status": "inProgress",
                            "items": [],
                        }
                    },
                }
            )
            answer = {
                "screen_title": f"Screen {thread_id}",
                "verification_code": "8HBG9-YWX82",
                "primary_button_label": "Inspect results",
            }
            self.emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": f"message-{thread_id}",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": json.dumps(answer),
                        },
                    },
                }
            )
            self.emit(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 120,
                                "cachedInputTokens": 80,
                                "outputTokens": 24,
                                "reasoningOutputTokens": 4,
                                "totalTokens": 148,
                            },
                            "total": {
                                "inputTokens": 120,
                                "cachedInputTokens": 80,
                                "outputTokens": 24,
                                "reasoningOutputTokens": 4,
                                "totalTokens": 148,
                            },
                        },
                    },
                }
            )
            self.emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": turn_id,
                            "status": "completed",
                            "items": [],
                            "error": None,
                        },
                    },
                }
            )
        elif method in {
            "thread/unsubscribe",
            "turn/interrupt",
        }:
            self.emit({"id": request_id, "result": {}})

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.lines.put_nowait(b"")

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


@pytest.mark.asyncio
async def test_persistent_codex_app_server_runs_concurrent_ephemeral_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(b"screen")
    process = FakeAppServerProcess()
    launches: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def create_process(
        *argv: str,
        **kwargs: Any,
    ) -> FakeAppServerProcess:
        launches.append((argv, kwargs))
        return process

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_process,
    )
    client = CodexAppServerClient(
        inherited_env=[],
    )
    schema = {
        "type": "object",
        "properties": {
            "screen_title": {"type": "string"},
            "verification_code": {"type": "string"},
            "primary_button_label": {"type": "string"},
        },
        "required": [
            "screen_title",
            "verification_code",
            "primary_button_label",
        ],
        "additionalProperties": False,
    }

    results = await asyncio.gather(
        *[
            client.complete(
                prompt=f"Inspect screen {index}",
                image_path=str(image),
                image_detail="high",
                output_schema=schema,
                model="gpt-5.6-luna",
                reasoning_effort="low",
                service_tier="priority",
                timeout_s=5,
            )
            for index in range(2)
        ]
    )
    await client.aclose()

    assert len(launches) == 1
    argv, launch_kwargs = launches[0]
    assert argv[:3] == ("codex", "app-server", "--stdio")
    assert launch_kwargs["env"] == {"NO_COLOR": "1"}
    for feature in (
        "apps",
        "computer_use",
        "hooks",
        "plugins",
        "shell_tool",
    ):
        position = argv.index(feature)
        assert argv[position - 1] == "--disable"
    assert [result.model for result in results] == [
        "gpt-5.6-luna",
        "gpt-5.6-luna",
    ]
    assert all(result.usage["cached_input_tokens"] == 80 for result in results)
    assert {json.loads(result.text)["screen_title"] for result in results} == {
        "Screen thread-1",
        "Screen thread-2",
    }
    thread_starts = [
        message
        for message in process.messages
        if message.get("method") == "thread/start"
    ]
    assert len(thread_starts) == 2
    assert all(
        message["params"]["ephemeral"] is True
        and message["params"]["approvalPolicy"] == "never"
        and message["params"]["sandbox"] == "read-only"
        and message["params"]["modelProvider"] == "openai"
        for message in thread_starts
    )
    turn_starts = [
        message
        for message in process.messages
        if message.get("method") == "turn/start"
    ]
    assert len(turn_starts) == 2
    assert all(
        message["params"]["effort"] == "low"
        and message["params"]["summary"] == "none"
        and message["params"]["serviceTier"] == "priority"
        and message["params"]["input"][1] == {
            "type": "localImage",
            "path": str(image),
            "detail": "high",
        }
        for message in turn_starts
    )
