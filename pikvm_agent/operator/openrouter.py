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
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        """One pooled client, reused across calls (a fresh client per decision would
        re-pay the TLS handshake every step). Injectable transport keeps tests offline."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(transport=self._transport, timeout=self._timeout)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

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
                    "image_url": {"url": f"data:image/jpeg;base64,{image}"},
                },
            ]
        return messages

    def _payload(self, model: str, messages: list[dict[str, Any]],
                 drop_format: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
        rf = self._response_format()
        if rf is not None and not drop_format:
            payload["response_format"] = rf
        return payload

    def _response_format(self) -> dict[str, Any] | None:
        """Structured-output mode per config. Defaults to the widely-supported
        ``json_object``; ``json_schema`` sends a strict schema (rejected by many
        providers, incl. Qwen-VL, with a 400); ``none`` omits it entirely."""
        mode = (self._config.structured_output or "json_object").lower()
        if mode == "none":
            return None
        if mode == "json_schema":
            return {"type": "json_schema", "json_schema": {
                "name": "operator_decision", "strict": True, "schema": decision_json_schema()}}
        return {"type": "json_object"}

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
        drop_format = False
        client = self._client()
        for _ in range(self._max_retries + 1):
            try:
                resp = await client.post(
                    url, headers=self._headers,
                    json=self._payload(model, messages, drop_format=drop_format),
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return validate_decision(content)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                # A 400 usually means the model/provider rejected response_format (a
                # strict json_schema, or json_object on a model that lacks it). Drop it
                # and let the prompt + our Pydantic validation enforce JSON instead.
                if exc.response.status_code == 400 and not drop_format:
                    drop_format = True
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
