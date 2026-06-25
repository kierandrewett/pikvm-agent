"""SQLite-backed session + approval records (async via aiosqlite).

Holds the durable spine of a session: its task, status, policy/operator config,
metrics, and the approval queue. Frame images and the event stream live on disk
(see frames.py / trace.py); this is the queryable index over them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import orjson

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    task        TEXT NOT NULL,
    status      TEXT NOT NULL,
    policy      TEXT NOT NULL DEFAULT '{}',
    operator    TEXT NOT NULL DEFAULT '{}',
    metrics     TEXT NOT NULL DEFAULT '{}',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    request     TEXT NOT NULL DEFAULT '{}',
    response    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id, status);
"""

_JSON_COLS = {"policy", "operator", "metrics", "request", "response"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in row.keys():
        val = row[key]
        if key in _JSON_COLS and isinstance(val, str):
            out[key] = orjson.loads(val) if val else None
        else:
            out[key] = val
    return out


class SessionStore:
    def __init__(self, sqlite_path: str | Path) -> None:
        self._path = Path(sqlite_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SessionStore.connect() not called")
        return self._db

    # ---- sessions --------------------------------------------------------- #

    async def create_session(self, session_id: str, task: str, policy: dict, operator: dict,
                             status: str = "running") -> dict[str, Any]:
        now = _now()
        await self.db.execute(
            "INSERT INTO sessions (id, task, status, policy, operator, metrics, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (session_id, task, status, orjson.dumps(policy).decode(),
             orjson.dumps(operator).decode(), "{}", now, now),
        )
        await self.db.commit()
        sess = await self.get_session(session_id)
        assert sess is not None
        return sess

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None

    async def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets, params = [], []
        for key, val in fields.items():
            sets.append(f"{key} = ?")
            params.append(orjson.dumps(val).decode() if key in _JSON_COLS else val)
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(session_id)
        await self.db.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)
        await self.db.commit()

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [_row_to_dict(r) for r in await cur.fetchall()]

    # ---- approvals -------------------------------------------------------- #

    async def save_approval(self, approval_id: str, session_id: str, request: dict) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO approvals (id, session_id, request, status, created_at)"
            " VALUES (?,?,?,?,?)",
            (approval_id, session_id, orjson.dumps(request).decode(), "pending", _now()),
        )
        await self.db.commit()

    async def resolve_approval(self, approval_id: str, response: dict, status: str) -> None:
        await self.db.execute(
            "UPDATE approvals SET response = ?, status = ?, resolved_at = ? WHERE id = ?",
            (orjson.dumps(response).decode(), status, _now(), approval_id),
        )
        await self.db.commit()

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None

    async def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM approvals WHERE session_id = ? AND status = 'pending' ORDER BY created_at",
            (session_id,),
        )
        return [_row_to_dict(r) for r in await cur.fetchall()]
