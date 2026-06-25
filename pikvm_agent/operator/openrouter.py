"""OpenRouter operator client.

The inner multimodal operator: it turns a frame + task into a structured,
schema-validated :class:`OperatorDecision`. It *proposes* guarded transactions;
it never executes anything. Structured JSON output is requested via OpenRouter's
``json_schema`` response format, and every response is parsed and Pydantic
validated before it leaves this module — a malformed response can never become
an action. Schema/validation/HTTP failures are retried with a corrective nudge.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx

from pikvm_agent.config import OperatorConfig
from pikvm_agent.core.errors import OperatorError
from pikvm_agent.core.models import OperatorDecision, OperatorRequest
from pikvm_agent.operator.models import select_lane
from pikvm_agent.operator.prompts import build_messages
from pikvm_agent.operator.schemas import decision_json_schema, validate_decision

__all__ = ["OpenRouterOperator", "DEFAULT_MAX_RETRIES"]

DEFAULT_MAX_RETRIES = 2


class OpenRouterOperator:
    """:class:`~pikvm_agent.core.ports.OperatorProvider` backed by OpenRouter."""

    def __init__(
        self,
        config: OperatorConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = 60.0,
    ) -> None:
        self._config = config
        self._transport = transport
        self._max_retries = max_retries
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        """A fresh client per call; injectable transport keeps tests offline."""
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout)

    def _build_messages(self, request: OperatorRequest) -> list[dict[str, Any]]:
        """Assemble chat messages, attaching the frame image when present.

        The base text-JSON payload comes from :func:`build_messages`. If the
        frame carries a non-empty base64 image, the final user message is turned
        into an OpenAI-style multimodal content list so the model sees both the
        task JSON and the pixels.
        """
        messages = copy.deepcopy(build_messages(request))
        image = request.frame.get("image")
        if isinstance(image, str) and image:
            user = messages[-1]
            user["content"] = [
                {"type": "text", "text": user["content"]},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image}"},
                },
            ]
        return messages

    def _payload(self, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "operator_decision",
                    "strict": True,
                    "schema": decision_json_schema(),
                },
            },
        }

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "HTTP-Referer": self._config.referer,
            "X-Title": self._config.title,
            "Content-Type": "application/json",
        }

    async def decide(
        self, request: OperatorRequest, *, lane: str = "default"
    ) -> OperatorDecision:
        """Propose one schema-validated decision for ``request``.

        Retries up to ``max_retries`` on HTTP/JSON/validation failure; on a
        schema failure it appends a corrective user message quoting the parse
        error and demanding strict JSON. Raises :class:`OperatorError` if no
        api key is configured (before any request) or once retries are spent.
        """
        if self._config.api_key is None:
            raise OperatorError("OPENROUTER_API_KEY not set")

        model = select_lane(self._config, lane)
        messages = self._build_messages(request)
        url = f"{self._config.base_url}/chat/completions"

        last_error: Exception | None = None
        async with self._client() as client:
            for _ in range(self._max_retries + 1):
                try:
                    resp = await client.post(
                        url, headers=self._headers, json=self._payload(model, messages)
                    )
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    return validate_decision(content)
                except OperatorError as exc:
                    # Schema/JSON failure: nudge the model toward strict JSON.
                    last_error = exc
                    messages = messages + [
                        {
                            "role": "user",
                            "content": (
                                "Your previous response could not be parsed: "
                                f"{exc}. Return only valid JSON matching the "
                                "operator_decision schema, with no prose, "
                                "markdown, or code fences."
                            ),
                        }
                    ]
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    last_error = exc

        raise OperatorError(
            f"operator failed after {self._max_retries + 1} attempts: {last_error}"
        ) from last_error
