"""Ultimate debug log — one JSONL stream of everything the daemon does, with timings.

Every line is one event: ``{"ts", "mono_ms", "kind", "session_id"?, "dur_ms"?, "ok"?,
...fields}``. It records LLM calls (request shape + response + latency), OmniParser /
OCR / screenshot / HID calls, every graph node, and every HTTP request — so "why is it
slow" is answered by reading the durations rather than guessing.

Two destinations, written together:
  * the daemon-wide log (everything), at ``debug_log_path``;
  * a per-TASK log at ``<session_dir>/<session_id>/debug.jsonl`` — so each task's story
    is isolated and easy to read.

A task is identified by a contextvar bound around a session's work (see
``bind_session``), so even calls that don't know the session id — the operator LLM, the
vision services, the screenshot fetch — are tagged and routed to the right task file.

Use it from anywhere via the module-level ``DEBUG`` singleton:

    from pikvm_agent.debuglog import DEBUG
    with DEBUG.span("operator.decide", model=model) as result:  # measures the await
        resp = await client.post(...)
        result(http_status=resp.status_code)

Tail it live:  tail -f <path> | jq .
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_session_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pikvm_debug_session", default=None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _DebugLog:
    def __init__(self) -> None:
        self._path: Path | None = None
        self._session_root: Path | None = None
        self._enabled = False
        self._lock = threading.Lock()
        self._t0 = time.monotonic()

    def configure(self, path: str | os.PathLike[str], *, session_dir: str | os.PathLike[str] | None = None,
                  enabled: bool = True, truncate: bool = False) -> None:
        """Point the daemon-wide log at ``path`` and per-task logs under ``session_dir``
        (``<session_dir>/<session_id>/debug.jsonl``). ``truncate`` starts the daemon-wide
        file fresh each boot."""
        self._enabled = enabled
        self._session_root = Path(session_dir) if session_dir else None
        if not enabled:
            self._path = None
            return
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if truncate and p.exists():
                p.unlink()
        except OSError:
            pass
        self._path = p
        self._t0 = time.monotonic()
        self.event("debug_log_open", pid=os.getpid(), path=str(p))

    @property
    def enabled(self) -> bool:
        return self._enabled and self._path is not None

    @property
    def path(self) -> Path | None:
        return self._path

    def set_session(self, session_id: str | None) -> None:
        """Tag subsequent events in THIS asyncio task with ``session_id`` (and route them
        to its per-task log). Each HTTP request runs in its own task with a fresh context,
        so setting at the top of a request handler needs no reset."""
        _session_ctx.set(session_id)

    @contextlib.contextmanager
    def bind_session(self, session_id: str | None):
        """Tag every event emitted inside this block (across awaits / threads / gather)
        with ``session_id`` and route it to that task's per-session log file too."""
        token = _session_ctx.set(session_id)
        try:
            yield
        finally:
            _session_ctx.reset(token)

    def _session_path(self, session_id: str) -> Path | None:
        if self._session_root is None:
            return None
        return self._session_root / session_id / "debug.jsonl"

    def event(self, kind: str, **fields: Any) -> None:
        if not self.enabled:
            return
        session_id = fields.pop("session_id", None) or _session_ctx.get()
        rec: dict[str, Any] = {
            "ts": _now_iso(),
            "mono_ms": round((time.monotonic() - self._t0) * 1000, 1),
            "kind": kind,
        }
        if session_id:
            rec["session_id"] = session_id
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        try:
            line = json.dumps(rec, default=_safe, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - never let logging raise into the hot path
            line = json.dumps({"ts": rec["ts"], "kind": kind, "log_error": "unserializable"})
        targets = [self._path]
        if session_id:
            sp = self._session_path(session_id)
            if sp is not None:
                targets.append(sp)
        with self._lock:
            for target in targets:
                if target is None:
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass

    @contextlib.contextmanager
    def span(self, kind: str, **fields: Any):
        """Time the wrapped block (works around awaits). Logs ``dur_ms`` + ``ok`` on exit;
        the yielded ``result(**extra)`` callback attaches fields known only at the end."""
        start = time.monotonic()
        extra: dict[str, Any] = {}

        def result(**kw: Any) -> None:
            extra.update(kw)

        err: str | None = None
        try:
            yield result
        except BaseException as exc:  # noqa: BLE001 - record then re-raise
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.event(kind, dur_ms=round((time.monotonic() - start) * 1000, 1),
                       ok=err is None, error=err, **fields, **extra)


def _safe(o: Any) -> str:
    s = str(o)
    return s if len(s) <= 500 else s[:500] + "…"


# Module-level singleton — import and use from anywhere in the daemon.
DEBUG = _DebugLog()
