from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pikvm_agent.harness.live_frames import (
    MAX_CACHED_LIVE_SESSIONS,
    MAX_LIVE_FRAME_BYTES,
    DaemonLiveFrameSource,
    LiveFrameRejected,
)


@pytest.mark.asyncio
async def test_live_frame_source_bounds_capture_rate_and_preserves_metadata() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/sessions/s_1/preview-frame"
        return httpx.Response(
            200,
            content=b"preview-jpeg",
            headers={
                "content-type": "image/jpeg",
                "x-pikvm-captured-at": "2026-07-24T18:00:00Z",
                "x-pikvm-width": "1280",
                "x-pikvm-height": "800",
            },
        )

    source = DaemonLiveFrameSource(
        "http://daemon",
        minimum_interval_s=5,
        transport=httpx.MockTransport(respond),
    )
    try:
        first, second = await asyncio.gather(
            source.get("s_1"),
            source.get("s_1"),
        )
    finally:
        await source.aclose()

    assert calls == 1
    assert first is second
    assert first.data == b"preview-jpeg"
    assert first.captured_at == "2026-07-24T18:00:00Z"
    assert (first.width, first.height) == (1280, 800)


@pytest.mark.asyncio
async def test_live_frame_source_rejects_declared_or_streamed_oversize() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                content=b"x",
                headers={
                    "content-type": "image/jpeg",
                    "content-length": "17",
                },
            ),
            httpx.Response(
                200,
                content=b"x" * 17,
                headers={
                    "content-type": "image/jpeg",
                    "content-length": "1",
                },
            ),
        ]
    )
    source = DaemonLiveFrameSource(
        "http://daemon",
        max_frame_bytes=16,
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )
    try:
        with pytest.raises(LiveFrameRejected, match="byte budget"):
            await source.get("declared")
        with pytest.raises(LiveFrameRejected, match="byte budget"):
            await source.get("streamed")
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_live_frame_source_rejects_non_image_and_invalid_dimensions() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                content=b"not-an-image",
                headers={"content-type": "text/html"},
            ),
            httpx.Response(
                200,
                content=b"image",
                headers={
                    "content-type": "image/jpeg",
                    "x-pikvm-width": "999999",
                    "x-pikvm-height": "800",
                },
            ),
        ]
    )
    source = DaemonLiveFrameSource(
        "http://daemon",
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )
    try:
        with pytest.raises(LiveFrameRejected, match="media type"):
            await source.get("html")
        with pytest.raises(LiveFrameRejected, match="width"):
            await source.get("dimensions")
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_live_frame_source_bounds_session_cache_and_lock_registry() -> None:
    calls: dict[str, int] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.split("/")[-2]
        calls[session_id] = calls.get(session_id, 0) + 1
        return httpx.Response(
            200,
            content=session_id.encode(),
            headers={"content-type": "image/jpeg"},
        )

    source = DaemonLiveFrameSource(
        "http://daemon",
        max_cached_sessions=2,
        transport=httpx.MockTransport(respond),
    )
    try:
        await source.get("s_1")
        await source.get("s_2")
        await source.get("s_3")
        assert source.cache_size == 2
        assert source.lock_count == 2
        await source.get("s_1")
        assert calls["s_1"] == 2
        assert source.cache_size == 2
        assert source.lock_count == 2
        assert source.cached_payload_bytes == len(b"s_3") + len(b"s_1")
    finally:
        await source.aclose()


def test_public_live_frame_resource_envelope_matches_runtime_constants() -> None:
    report_path = (
        Path(__file__).parents[1]
        / "bench"
        / "results"
        / "2026-07-25"
        / "ui"
        / "live-frame-resource-envelope-2026-07-26.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    envelope = report["resource_envelope"]

    assert report["contracts"] == {"passed": 6, "total": 6}
    assert envelope["max_frame_bytes"] == MAX_LIVE_FRAME_BYTES
    assert envelope["max_cached_sessions"] == MAX_CACHED_LIVE_SESSIONS
    assert envelope["max_cached_payload_bytes"] == (
        MAX_LIVE_FRAME_BYTES * MAX_CACHED_LIVE_SESSIONS
    )
