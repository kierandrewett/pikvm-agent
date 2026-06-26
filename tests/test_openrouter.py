"""Tests for the OpenRouter operator client — fully offline.

Every test injects an :class:`httpx.MockTransport`, so no real network is
touched. Covers the happy path (valid decision + outgoing request shape),
schema-failure retry, exhausted retries, the missing-api-key guard (zero
requests), and image attachment as an OpenAI-style content block.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pikvm_agent.config import OperatorConfig
from pikvm_agent.core.errors import OperatorError
from pikvm_agent.core.models import OperatorDecision, OperatorRequest
from pikvm_agent.operator.models import select_lane
from pikvm_agent.operator.openrouter import OpenRouterOperator


def _request(image: str = "") -> OperatorRequest:
    return OperatorRequest(
        task="open quick open",
        frame={"id": 18429, "world_version": 702, "image": image, "age_ms": 50},
    )


def _valid_decision_json() -> str:
    return json.dumps(
        {
            "based_on_frame_id": 18429,
            "based_on_world_version": 702,
            "intent": "Open VS Code Quick Open.",
            "state_assessment": {"active_app": "vscode"},
            "risk": {
                "level": "low",
                "category": "navigation",
                "requires_human": False,
                "reason": "",
            },
            "preconditions": {"no_blocking_popup": True},
            "actions": [{"type": "keypress", "keys": ["CTRL", "P"]}],
            "postconditions": {"verify_mode": "vscode.quick_open"},
            "fallback": "reobserve",
        }
    )


def _choice(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_decide_returns_decision_and_sends_expected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = OperatorConfig()
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        return _choice(_valid_decision_json())

    op = OpenRouterOperator(config, transport=httpx.MockTransport(handler))
    decision = await op.decide(_request())

    assert isinstance(decision, OperatorDecision)
    assert decision.based_on_frame_id == 18429
    assert decision.based_on_world_version == 702

    assert seen["url"] == f"{config.base_url}/chat/completions"
    assert "response_format" in seen["body"]
    assert seen["body"]["model"] == select_lane(config, "default")
    assert seen["headers"]["authorization"] == "Bearer test-key"


async def test_decide_retries_after_malformed_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _choice("{not valid json")
        return _choice(_valid_decision_json())

    op = OpenRouterOperator(OperatorConfig(), transport=httpx.MockTransport(handler))
    decision = await op.decide(_request())

    assert decision.based_on_frame_id == 18429
    assert calls["n"] == 2


async def test_decide_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _choice("totally not json")

    op = OpenRouterOperator(OperatorConfig(), transport=httpx.MockTransport(handler))
    with pytest.raises(OperatorError):
        await op.decide(_request())

    # Initial attempt + DEFAULT_MAX_RETRIES (2) = 3 calls.
    assert calls["n"] == 3


async def test_decide_without_api_key_makes_zero_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls["n"] += 1
        return _choice(_valid_decision_json())

    op = OpenRouterOperator(OperatorConfig(), transport=httpx.MockTransport(handler))
    with pytest.raises(OperatorError):
        await op.decide(_request())

    assert calls["n"] == 0


async def test_decide_attaches_image_as_content_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    b64 = "aGVsbG8="  # "hello"
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return _choice(_valid_decision_json())

    op = OpenRouterOperator(OperatorConfig(), transport=httpx.MockTransport(handler))
    await op.decide(_request(image=b64))

    messages = seen["body"]["messages"]
    user = messages[-1]
    assert isinstance(user["content"], list)
    image_blocks = [b for b in user["content"] if b.get("type") == "image_url"]
    assert image_blocks
    assert image_blocks[0]["image_url"]["url"] == f"data:image/jpeg;base64,{b64}"
    # The image must NOT also be duplicated into the text-JSON block.
    text_blocks = [b for b in user["content"] if b.get("type") == "text"]
    assert b64 not in text_blocks[0]["text"]


async def test_decide_defaults_to_json_object_and_falls_back_on_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body.get("response_format"))
        # First call (json_object) is rejected with a 400; the client must drop
        # response_format and retry, which then succeeds.
        if len(seen) == 1:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return _choice(_valid_decision_json())

    op = OpenRouterOperator(OperatorConfig(), transport=httpx.MockTransport(handler))
    decision = await op.decide(_request())

    assert decision.based_on_frame_id == 18429
    assert seen[0] == {"type": "json_object"}  # default mode
    assert "response_format" not in str(seen[1])  # dropped on the 400 retry
    assert seen[1] is None
