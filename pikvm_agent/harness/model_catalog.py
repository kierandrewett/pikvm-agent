"""Public model catalog, sourced from models.dev.

The workspace needs real model names, costs, context limits and provider
branding to make picking a model feel like choosing from a menu rather than
editing a config file. models.dev maintains exactly that catalog, so we cache
it here and re-shape it around OUR provider kinds: the UI asks "what can a
claude_cli account run", not "what does Anthropic sell".

Design constraints:
  - Never block a request on the network: fetches happen at most once per TTL,
    failures fall back to the last good snapshot on disk, and a harness that
    has never been online simply reports an empty catalog.
  - No secrets and no user data leave the harness; the only outbound request
    is the public catalog JSON.
  - Logos are referenced by URL (models.dev serves per-provider SVGs); the UI
    must degrade to initials when offline.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx

MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_LOGO_URL = "https://models.dev/logos/{provider_id}.svg"
# The UI must load logos from the harness, not from models.dev: its CSP is
# `img-src 'self' blob:`, so a remote URL is blocked before a request is made.
# Proxying keeps that policy intact and keeps the page from talking to anyone.
LOGO_PATH_TEMPLATE = "/api/model-catalog/logo/{provider_id}"
CACHE_TTL_S = 24 * 3600
FETCH_TIMEOUT_S = 15
MAX_LOGO_BYTES = 256 * 1024

# Our provider kinds -> the models.dev provider ids whose model lists apply.
# A CLI subscription and the matching API kind draw from the same menu; the
# authentication difference lives in provider_support, not here.
KIND_TO_MODELS_DEV: Mapping[str, tuple[str, ...]] = {
    "codex_cli": ("openai",),
    "codex_app_server": ("openai",),
    "openai_responses": ("openai",),
    "openai_compatible": ("openai", "openrouter"),
    "azure_openai_responses": ("azure",),
    "claude_cli": ("anthropic",),
    "anthropic_api": ("anthropic",),
    "gemini_cli": ("google",),
    "gemini_api": ("google",),
    "vertex_gemini": ("google-vertex", "google"),
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _public_model(model: Mapping[str, Any]) -> dict[str, Any] | None:
    """Re-shape one models.dev model entry; None if it cannot drive the agent."""

    model_id = model.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    # The operator loop needs tool calls; a model that cannot call tools cannot
    # act on the computer, so listing it would only invite a broken pick.
    if model.get("tool_call") is not True:
        return None
    limit = _as_mapping(model.get("limit"))
    cost = _as_mapping(model.get("cost"))
    modalities = _as_mapping(model.get("modalities"))
    inputs = modalities.get("input")
    return {
        "id": model_id,
        "name": model.get("name") or model_id,
        "family": model.get("family"),
        "reasoning": model.get("reasoning") is True,
        "image_input": isinstance(inputs, list) and "image" in inputs,
        "context": limit.get("context"),
        "output_limit": limit.get("output"),
        "cost_input": cost.get("input"),
        "cost_output": cost.get("output"),
        "release_date": model.get("release_date"),
    }


def _release_key(model: Mapping[str, Any]) -> str:
    release = model.get("release_date")
    return release if isinstance(release, str) else ""


def public_catalog(
    raw: Mapping[str, Any],
    *,
    kinds: tuple[str, ...] | None = None,
    max_models_per_provider: int = 60,
) -> dict[str, Any]:
    """Filter the raw models.dev document down to what our adapters can use.

    Returns {providers: {models_dev_id: {...}}, kinds: {our_kind: [ids]}} so
    the UI can go from a configured provider's kind straight to a model menu.
    """

    wanted_kinds = kinds if kinds is not None else tuple(KIND_TO_MODELS_DEV)
    needed_ids: dict[str, None] = {}  # ordered set
    kind_map: dict[str, list[str]] = {}
    for kind in wanted_kinds:
        ids = [pid for pid in KIND_TO_MODELS_DEV.get(kind, ()) if pid in raw]
        kind_map[kind] = ids
        for pid in ids:
            needed_ids.setdefault(pid)

    providers: dict[str, Any] = {}
    for pid in needed_ids:
        entry = _as_mapping(raw.get(pid))
        models_raw = _as_mapping(entry.get("models"))
        models = [
            public
            for model in models_raw.values()
            if (public := _public_model(_as_mapping(model))) is not None
        ]
        models.sort(key=_release_key, reverse=True)
        providers[pid] = {
            "id": pid,
            "name": entry.get("name") or pid,
            "logo_url": LOGO_PATH_TEMPLATE.format(provider_id=pid),
            "models": models[:max_models_per_provider],
        }
    return {"providers": providers, "kinds": kind_map}


async def _default_fetch() -> Mapping[str, Any]:
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
        response = await client.get(MODELS_DEV_URL)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, Mapping):
            raise ValueError("models.dev returned a non-object document")
        return data


@dataclass
class ModelCatalogService:
    """Cached read-through to models.dev.

    `cache_path` holds the last good RAW document so restarts and offline
    periods keep a working catalog. `fetch` is injectable for tests.
    """

    cache_path: Path
    ttl_s: float = CACHE_TTL_S
    fetch: Callable[[], Awaitable[Mapping[str, Any]]] = field(
        default=_default_fetch,
    )
    _raw: Mapping[str, Any] | None = field(default=None, init=False)
    _fetched_at: float = field(default=0.0, init=False)
    _last_error: str | None = field(default=None, init=False)
    _logos: dict[str, bytes | None] = field(default_factory=dict, init=False)

    def _load_disk(self) -> None:
        if self._raw is not None:
            return
        try:
            stored = json.loads(self.cache_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        raw = stored.get("raw") if isinstance(stored, dict) else None
        fetched_at = stored.get("fetched_at") if isinstance(stored, dict) else None
        if isinstance(raw, dict):
            self._raw = raw
            self._fetched_at = float(fetched_at or 0)

    def _store_disk(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"fetched_at": self._fetched_at, "raw": self._raw}),
                "utf-8",
            )
        except OSError:
            # Cache persistence is best-effort; memory still has the document.
            pass

    async def refresh_if_stale(self) -> None:
        self._load_disk()
        if self._raw is not None and time.time() - self._fetched_at < self.ttl_s:
            return
        try:
            raw = await self.fetch()
        except Exception as exc:  # noqa: BLE001 - degrade, never break the API
            self._last_error = f"{type(exc).__name__}: {exc}"
            return
        self._raw = raw
        self._fetched_at = time.time()
        self._last_error = None
        self._store_disk()

    async def logo(self, provider_id: str) -> bytes | None:
        """The provider's SVG, fetched once and kept in memory.

        Only ids present in the cached catalog are fetched, so this cannot be
        pointed at an arbitrary URL by a caller.
        """

        if provider_id in self._logos:
            return self._logos[provider_id]
        await self.refresh_if_stale()
        if self._raw is None or provider_id not in self._raw:
            return None
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
                response = await client.get(
                    MODELS_DEV_LOGO_URL.format(provider_id=provider_id)
                )
                response.raise_for_status()
                body = response.content
        except Exception:  # noqa: BLE001 - a missing logo is not an error
            self._logos[provider_id] = None
            return None
        if len(body) > MAX_LOGO_BYTES or b"<svg" not in body[:512].lower():
            # Not an SVG, or implausibly large for one.
            self._logos[provider_id] = None
            return None
        self._logos[provider_id] = body
        return body

    async def snapshot(self) -> dict[str, Any]:
        """The API response: available catalog or an honest 'not available'."""

        await self.refresh_if_stale()
        if self._raw is None:
            return {
                "available": False,
                "fetched_at": None,
                **({"error": self._last_error} if self._last_error else {}),
                "providers": {},
                "kinds": {},
            }
        catalog = public_catalog(self._raw)
        return {
            "available": True,
            "fetched_at": self._fetched_at,
            **catalog,
        }
