"""Low-latency read-only frame adapter for the operator console."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote

import httpx

MAX_LIVE_FRAME_BYTES = 4 * 1024 * 1024
MAX_LIVE_FRAME_DIMENSION = 8192
MAX_CACHED_LIVE_SESSIONS = 8
ALLOWED_LIVE_FRAME_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)


@dataclass(frozen=True)
class LiveFrame:
    data: bytes
    media_type: str
    captured_at: str
    width: int | None
    height: int | None


class LiveFrameRejected(ValueError):
    """A preview response exceeded the operator-console resource contract."""


class DaemonLiveFrameSource:
    """Fetch preview snapshots without entering the action MCP call queue.

    Computer mutations still flow through the raw MCP server.  Preview frames
    use the daemon's read-only endpoint so a slow burst cannot blind the
    operator and base64 image payloads do not traverse MCP.  A short cache
    bounds upstream capture rate when multiple UI clients watch one session.
    """

    def __init__(
        self,
        daemon_url: str,
        *,
        bearer_token: str | None = None,
        minimum_interval_s: float = 0.45,
        timeout_s: float = 8.0,
        max_frame_bytes: int = MAX_LIVE_FRAME_BYTES,
        max_cached_sessions: int = MAX_CACHED_LIVE_SESSIONS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if minimum_interval_s < 0:
            raise ValueError("minimum_interval_s cannot be negative")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be positive")
        if max_cached_sessions < 1:
            raise ValueError("max_cached_sessions must be positive")
        self._client = httpx.AsyncClient(
            base_url=daemon_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers=(
                {"Authorization": f"Bearer {bearer_token}"}
                if bearer_token
                else None
            ),
        )
        self.minimum_interval_s = minimum_interval_s
        self.max_frame_bytes = max_frame_bytes
        self.max_cached_sessions = max_cached_sessions
        self._cache: OrderedDict[str, tuple[float, LiveFrame]] = OrderedDict()
        self._locks = tuple(
            asyncio.Lock() for _ in range(max_cached_sessions)
        )

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    @property
    def cached_payload_bytes(self) -> int:
        return sum(len(frame.data) for _, frame in self._cache.values())

    async def get(self, session_id: str) -> LiveFrame:
        now = time.monotonic()
        cached = self._fresh_cached(session_id, now)
        if cached is not None:
            return cached
        lock = self._locks[hash(session_id) % len(self._locks)]
        async with lock:
            now = time.monotonic()
            cached = self._fresh_cached(session_id, now)
            if cached is not None:
                return cached
            frame = await self._fetch(session_id)
            self._store(session_id, frame)
            return frame

    async def aclose(self) -> None:
        await self._client.aclose()
        self._cache.clear()

    def _fresh_cached(self, session_id: str, now: float) -> LiveFrame | None:
        cached = self._cache.get(session_id)
        if cached is None or now - cached[0] >= self.minimum_interval_s:
            return None
        self._cache.move_to_end(session_id)
        return cached[1]

    def _store(self, session_id: str, frame: LiveFrame) -> None:
        self._cache[session_id] = (time.monotonic(), frame)
        self._cache.move_to_end(session_id)
        while len(self._cache) > self.max_cached_sessions:
            self._cache.popitem(last=False)

    async def _fetch(self, session_id: str) -> LiveFrame:
        async with self._client.stream(
            "GET",
            f"/sessions/{quote(session_id, safe='')}/preview-frame",
            headers={"Accept": "image/*"},
        ) as response:
            response.raise_for_status()
            media_type = response.headers.get(
                "content-type", "image/jpeg"
            ).split(";", 1)[0].strip().lower()
            if media_type not in ALLOWED_LIVE_FRAME_MEDIA_TYPES:
                raise LiveFrameRejected(
                    f"unsupported live frame media type: {media_type or 'missing'}"
                )
            declared_length = _optional_int(response.headers.get("content-length"))
            if declared_length is not None and declared_length > self.max_frame_bytes:
                raise LiveFrameRejected("live frame exceeds byte budget")
            width = _optional_int(response.headers.get("x-pikvm-width"))
            height = _optional_int(response.headers.get("x-pikvm-height"))
            for label, value in (("width", width), ("height", height)):
                if value is not None and (
                    value <= 0 or value > MAX_LIVE_FRAME_DIMENSION
                ):
                    raise LiveFrameRejected(
                        f"live frame {label} is outside the accepted range"
                    )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self.max_frame_bytes:
                    raise LiveFrameRejected("live frame exceeds byte budget")
            if not data:
                raise LiveFrameRejected("live frame is empty")
            return LiveFrame(
                data=bytes(data),
                media_type=media_type,
                captured_at=response.headers.get("x-pikvm-captured-at", ""),
                width=width,
                height=height,
            )


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None
