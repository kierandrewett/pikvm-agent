"""Session-bound daemon adapter for harness-owned media transactions."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Any
from urllib.parse import quote

import httpx

from pikvm_agent.endpoint import httpx_client_kwargs
from pikvm_agent.harness.media_transaction import (
    MediaMutationAmbiguousError,
    MediaMutationDefiniteError,
    MediaTargetState,
)


class DaemonMediaTransport:
    """Send exact media bytes to our daemon; never contact PiKVM directly."""

    def __init__(
        self,
        daemon_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            **httpx_client_kwargs(daemon_url, transport),
            timeout=timeout,
        )

    async def inspect(self, session_id: str) -> MediaTargetState:
        try:
            response = await self._client.get(
                f"/sessions/{self._session(session_id)}/media"
            )
            response.raise_for_status()
            return MediaTargetState.model_validate(response.json())
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise RuntimeError(
                f"daemon media inspection failed: {self._detail(exc)}"
            ) from exc

    async def upload(
        self,
        session_id: str,
        name: str,
        image_path: Path,
    ) -> None:
        await self._mutation(
            "POST",
            f"/sessions/{self._session(session_id)}/media/upload",
            params={"image": name},
            content=self._file_chunks(image_path),
        )

    async def select(self, session_id: str, name: str | None) -> None:
        await self._mutation(
            "POST",
            f"/sessions/{self._session(session_id)}/media/select",
            json={
                "image": name,
                "cdrom": True,
                "read_only": True,
            },
        )

    async def connect(self, session_id: str) -> None:
        await self._set_connected(session_id, True)

    async def disconnect(self, session_id: str) -> None:
        await self._set_connected(session_id, False)

    async def remove(self, session_id: str, name: str) -> None:
        await self._mutation(
            "POST",
            f"/sessions/{self._session(session_id)}/media/remove",
            json={"image": name},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _set_connected(
        self,
        session_id: str,
        connected: bool,
    ) -> None:
        await self._mutation(
            "POST",
            f"/sessions/{self._session(session_id)}/media/connected",
            json={"connected": connected},
        )

    async def _mutation(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> None:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise MediaMutationAmbiguousError(
                f"daemon media response was lost: {self._detail(exc)}"
            ) from exc
        if response.status_code >= 500:
            raise MediaMutationAmbiguousError(self._response_detail(response))
        if response.is_error:
            raise MediaMutationDefiniteError(self._response_detail(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise MediaMutationAmbiguousError(
                "daemon media mutation returned invalid confirmation"
            ) from exc
        if payload.get("outcome") != "confirmed":
            raise MediaMutationAmbiguousError(
                "daemon media mutation did not confirm its outcome"
            )

    @staticmethod
    async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                yield chunk

    @staticmethod
    def _session(session_id: str) -> str:
        value = session_id.strip()
        if not value or len(value) > 200:
            raise ValueError("invalid media session id")
        return quote(value, safe="")

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"daemon returned HTTP {response.status_code}"
        return str(
            payload.get("detail")
            or payload.get("error")
            or f"daemon returned HTTP {response.status_code}"
        )[:500]

    @staticmethod
    def _detail(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return DaemonMediaTransport._response_detail(exc.response)
        return str(exc)[:500] or type(exc).__name__
