"""The models.dev catalog: transform, caching, and degradation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pikvm_agent.harness.model_catalog import (
    ModelCatalogService,
    public_catalog,
)

RAW: Mapping[str, Any] = {
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "models": {
            "claude-opus-5": {
                "id": "claude-opus-5",
                "name": "Claude Opus 5",
                "family": "claude",
                "tool_call": True,
                "reasoning": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 500000, "output": 128000},
                "cost": {"input": 5, "output": 25},
                "release_date": "2026-05-01",
            },
            "claude-haiku-4-5": {
                "id": "claude-haiku-4-5",
                "name": "Claude Haiku 4.5",
                "tool_call": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 200000, "output": 64000},
                "cost": {"input": 1, "output": 5},
                "release_date": "2025-10-01",
            },
            "no-tools-model": {
                "id": "no-tools-model",
                "name": "Chat-only",
                "tool_call": False,
            },
        },
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "models": {},
    },
}


def test_public_catalog_maps_kinds_and_filters_models() -> None:
    catalog = public_catalog(RAW, kinds=("claude_cli", "anthropic_api"))

    assert catalog["kinds"] == {
        "claude_cli": ["anthropic"],
        "anthropic_api": ["anthropic"],
    }
    provider = catalog["providers"]["anthropic"]
    assert provider["name"] == "Anthropic"
    # Harness-relative, not models.dev: the UI's CSP is `img-src 'self'`, so a
    # remote URL would be blocked before a request was ever made.
    assert provider["logo_url"] == "/api/model-catalog/logo/anthropic"
    ids = [model["id"] for model in provider["models"]]
    # Newest first, and the tool-less model is excluded: a model that cannot
    # call tools cannot act on the computer, so listing it invites a broken pick.
    assert ids == ["claude-opus-5", "claude-haiku-4-5"]
    opus = provider["models"][0]
    assert opus["image_input"] is True
    assert opus["context"] == 500000
    assert opus["cost_output"] == 25


def test_unknown_models_dev_providers_are_skipped() -> None:
    catalog = public_catalog(RAW, kinds=("vertex_gemini",))
    # Neither google-vertex nor google exist in this document.
    assert catalog["kinds"] == {"vertex_gemini": []}
    assert catalog["providers"] == {}


def test_snapshot_fetches_once_then_serves_from_cache(tmp_path: Path) -> None:
    calls = 0

    async def fetch() -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        return RAW

    service = ModelCatalogService(cache_path=tmp_path / "cache.json", fetch=fetch)
    first = asyncio.run(service.snapshot())
    second = asyncio.run(service.snapshot())

    assert first["available"] is True
    assert "anthropic" in first["providers"]
    assert second["available"] is True
    assert calls == 1, "within the TTL the network must not be touched again"


def test_snapshot_survives_network_failure_via_disk_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"fetched_at": 1, "raw": dict(RAW)}), "utf-8")

    async def fetch() -> Mapping[str, Any]:
        raise OSError("offline")

    # fetched_at=1 is decades stale, so a refresh is attempted and fails —
    # the stale disk snapshot must still serve.
    service = ModelCatalogService(cache_path=cache, fetch=fetch)
    snapshot = asyncio.run(service.snapshot())
    assert snapshot["available"] is True
    assert "anthropic" in snapshot["providers"]


def test_logo_is_only_fetched_for_providers_in_the_catalog(tmp_path: Path) -> None:
    asked: list[str] = []

    class FakeResponse:
        content = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            asked.append(url)
            return FakeResponse()

    import pikvm_agent.harness.model_catalog as module

    original = module.httpx.AsyncClient
    module.httpx.AsyncClient = FakeClient  # type: ignore[assignment]
    try:
        service = ModelCatalogService(
            cache_path=tmp_path / "cache.json",
            fetch=lambda: _immediate(RAW),
        )
        assert asyncio.run(service.logo("anthropic")) is not None
        # Cached: a second read must not hit the network again.
        assert asyncio.run(service.logo("anthropic")) is not None
        assert len(asked) == 1
        # An id absent from the catalog is never fetched, so this cannot be
        # pointed at an arbitrary URL by a caller.
        assert asyncio.run(service.logo("not-a-provider")) is None
        assert len(asked) == 1
    finally:
        module.httpx.AsyncClient = original  # type: ignore[assignment]


async def _immediate(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return value


def test_snapshot_degrades_honestly_when_never_fetched(tmp_path: Path) -> None:
    async def fetch() -> Mapping[str, Any]:
        raise OSError("offline")

    service = ModelCatalogService(cache_path=tmp_path / "cache.json", fetch=fetch)
    snapshot = asyncio.run(service.snapshot())
    assert snapshot["available"] is False
    assert snapshot["providers"] == {}
    assert "OSError" in snapshot["error"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
