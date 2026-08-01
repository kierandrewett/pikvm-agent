"""Durable run state with append-only event storage behind one interface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Protocol

import aiosqlite

from pikvm_agent.harness.agent_models import (
    HarnessEvent,
    RunEventPage,
    RunSnapshot,
    RunSummary,
    VERIFICATION_SUMMARY_MAX_LENGTH,
)

CONTROL_EVENT_TAIL_LIMIT = 1_000
SQLITE_BUSY_TIMEOUT_SECONDS = 15.0


class RunNotFoundError(KeyError):
    pass


class RunHistoryConflictError(RuntimeError):
    """A caller attempted to replace or truncate append-only run history."""


class RunStore(Protocol):
    async def save(self, run: RunSnapshot) -> None: ...
    async def get(self, run_id: str) -> RunSnapshot: ...
    async def get_control(
        self,
        run_id: str,
        event_limit: int = CONTROL_EVENT_TAIL_LIMIT,
    ) -> RunSnapshot: ...
    async def get_state(self, run_id: str) -> RunSnapshot: ...
    async def get_summary(self, run_id: str) -> RunSummary: ...
    async def events_after(
        self,
        run_id: str,
        after: int,
        limit: int,
    ) -> RunEventPage: ...
    async def events_matching(
        self,
        run_id: str,
        kinds: frozenset[str],
        limit: int,
    ) -> RunEventPage: ...
    async def updates_after(
        self,
        run_id: str,
        after: int,
        limit: int,
    ) -> tuple[RunSummary, RunEventPage]: ...
    async def list(self, limit: int = 100) -> list[RunSnapshot]: ...
    async def list_summaries(self, limit: int = 100) -> list[RunSummary]: ...


def _state_json(run: RunSnapshot) -> str:
    return json.dumps(
        run.model_dump(mode="json", exclude={"events"}),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _summary_json(run: RunSnapshot) -> str:
    return RunSummary.from_snapshot(run).model_dump_json()


def _normalize_legacy_state(payload: dict[str, object]) -> None:
    """Keep pre-bound verification summaries readable after an upgrade."""
    verification = payload.get("last_verification")
    if not isinstance(verification, dict):
        return
    summary = verification.get("summary")
    if (
        not isinstance(summary, str)
        or len(summary) <= VERIFICATION_SUMMARY_MAX_LENGTH
    ):
        return
    verification["summary"] = (
        summary[: VERIFICATION_SUMMARY_MAX_LENGTH - 1] + "…"
    )


def _snapshot_from_parts(
    state_json: str,
    event_jsons: list[str],
    *,
    event_cursor: int | None = None,
) -> RunSnapshot:
    payload = json.loads(state_json)
    _normalize_legacy_state(payload)
    legacy_events = payload.pop("events", [])
    events = (
        [json.loads(event_json) for event_json in event_jsons]
        if event_jsons
        else legacy_events
    )
    payload["events"] = events
    loaded_cursor = (
        int(events[-1]["sequence"])
        if events
        else int(payload.get("event_cursor") or 0)
    )
    payload["event_cursor"] = max(
        loaded_cursor,
        int(event_cursor or 0),
        int(payload.get("event_cursor") or 0),
    )
    return RunSnapshot.model_validate(payload)


def _new_event_jsons(
    existing_count: int,
    existing_last_json: str | None,
    events: list[HarnessEvent],
    *,
    event_cursor: int | None = None,
) -> list[str]:
    if (
        events
        and event_cursor is not None
        and events[-1].sequence != event_cursor
    ):
        if events[-1].sequence > event_cursor:
            raise RunHistoryConflictError(
                "provided event window is not a contiguous suffix"
            )
        raise RunHistoryConflictError(
            "run history cannot be truncated before the declared cursor"
        )
    if existing_count:
        if existing_last_json is None:
            raise RunHistoryConflictError(
                "durable event history has no terminal event"
            )
        durable_last = HarnessEvent.model_validate_json(
            existing_last_json
        )
        if durable_last.sequence != existing_count:
            raise RunHistoryConflictError(
                "durable event history is not contiguous"
            )
        terminal_index = (
            existing_count - events[0].sequence if events else -1
        )
        matching_terminal = (
            events[terminal_index]
            if 0 <= terminal_index < len(events)
            else None
        )
        if matching_terminal is not None and durable_last != matching_terminal:
            raise RunHistoryConflictError(
                "run history cannot replace an existing durable event"
            )
    if events:
        new_start_index = max(
            0,
            existing_count + 1 - events[0].sequence,
        )
        new_events = events[new_start_index:]
    else:
        new_events = []
    for expected_sequence, event in enumerate(
        new_events,
        start=existing_count + 1,
    ):
        if event.sequence != expected_sequence:
            raise RunHistoryConflictError(
                "new run events must form one contiguous suffix"
            )
    declared_cursor = (
        event_cursor
        if event_cursor is not None
        else (events[-1].sequence if events else existing_count)
    )
    expected_cursor = existing_count + len(new_events)
    if declared_cursor != expected_cursor:
        raise RunHistoryConflictError(
            "run event cursor does not match its durable append suffix"
        )
    return [
        event.model_dump_json()
        for event in new_events
    ]


class InMemoryRunStore:
    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._events: dict[str, list[str]] = {}
        self._summaries: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def save(self, run: RunSnapshot) -> None:
        async with self._lock:
            durable_events = self._events.get(run.run_id, [])
            durable_events.extend(
                _new_event_jsons(
                    len(durable_events),
                    durable_events[-1] if durable_events else None,
                    run.events,
                    event_cursor=run.event_cursor,
                )
            )
            self._states[run.run_id] = _state_json(run)
            self._events[run.run_id] = durable_events
            self._summaries[run.run_id] = _summary_json(run)

    async def get(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            state_json = self._states.get(run_id)
            event_jsons = list(self._events.get(run_id, []))
        if state_json is None:
            raise RunNotFoundError(run_id)
        return _snapshot_from_parts(
            state_json,
            event_jsons,
            event_cursor=len(event_jsons),
        )

    async def get_control(
        self,
        run_id: str,
        event_limit: int = CONTROL_EVENT_TAIL_LIMIT,
    ) -> RunSnapshot:
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        async with self._lock:
            state_json = self._states.get(run_id)
            durable_events = self._events.get(run_id, [])
            event_jsons = list(durable_events[-event_limit:])
            event_cursor = len(durable_events)
        if state_json is None:
            raise RunNotFoundError(run_id)
        return _snapshot_from_parts(
            state_json,
            event_jsons,
            event_cursor=event_cursor,
        )

    async def get_state(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            state_json = self._states.get(run_id)
            summary_json = self._summaries.get(run_id)
        if state_json is None:
            raise RunNotFoundError(run_id)
        if summary_json is None:  # pragma: no cover - state/summary are atomic
            raise RunNotFoundError(run_id)
        return _snapshot_from_parts(
            state_json,
            [],
            event_cursor=RunSummary.model_validate_json(
                summary_json
            ).event_cursor,
        )

    async def get_summary(self, run_id: str) -> RunSummary:
        async with self._lock:
            raw = self._summaries.get(run_id)
        if raw is None:
            raise RunNotFoundError(run_id)
        return RunSummary.model_validate_json(raw)

    async def events_after(
        self,
        run_id: str,
        after: int,
        limit: int,
    ) -> RunEventPage:
        _, page = await self.updates_after(run_id, after, limit)
        return page

    async def events_matching(
        self,
        run_id: str,
        kinds: frozenset[str],
        limit: int,
    ) -> RunEventPage:
        if not kinds:
            raise ValueError("kinds must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._lock:
            if run_id not in self._states:
                raise RunNotFoundError(run_id)
            durable_events = self._events.get(run_id, [])
            selected = [
                raw
                for raw in durable_events
                if json.loads(raw).get("kind") in kinds
            ][: limit + 1]
        return RunEventPage(
            events=[
                HarnessEvent.model_validate_json(raw)
                for raw in selected[:limit]
            ],
            latest_cursor=len(durable_events),
            has_more=len(selected) > limit,
        )

    async def updates_after(
        self,
        run_id: str,
        after: int,
        limit: int,
    ) -> tuple[RunSummary, RunEventPage]:
        async with self._lock:
            if run_id not in self._states:
                raise RunNotFoundError(run_id)
            durable_events = self._events.get(run_id, [])
            selected = durable_events[after : after + limit + 1]
            summary_json = self._summaries[run_id]
        return RunSummary.model_validate_json(summary_json), RunEventPage(
            events=[
                HarnessEvent.model_validate_json(raw)
                for raw in selected[:limit]
            ],
            latest_cursor=len(durable_events),
            has_more=len(selected) > limit,
        )

    async def list(self, limit: int = 100) -> list[RunSnapshot]:
        summaries = await self.list_summaries(limit=limit)
        return [await self.get(summary.run_id) for summary in summaries]

    async def list_summaries(self, limit: int = 100) -> list[RunSummary]:
        async with self._lock:
            summaries = [
                RunSummary.model_validate_json(raw)
                for raw in self._summaries.values()
            ]
        return sorted(
            summaries,
            key=lambda run: run.updated_at,
            reverse=True,
        )[:limit]


class SqliteRunStore:
    """Atomic run checkpoints plus normalized append-only event rows.

    The current non-event state remains one atomic JSON document so a pending
    action and its idempotency key become durable together before HID. Events
    are stored separately and appended once, allowing the run rail to read
    summaries without loading or rewriting complete history.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        ) as db:
            await db.execute(
                f"PRAGMA busy_timeout = "
                f"{int(SQLITE_BUSY_TIMEOUT_SECONDS * 1_000)}"
            )
            yield db

    async def _initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            async with self._connection() as db:
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_runs (
                        run_id TEXT PRIMARY KEY,
                        state_json TEXT NOT NULL,
                        summary_json TEXT NOT NULL DEFAULT '{}',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                async with db.execute(
                    "PRAGMA table_info(harness_runs)"
                ) as cursor:
                    columns = {
                        row[1] for row in await cursor.fetchall()
                    }
                if "summary_json" not in columns:
                    await db.execute(
                        """
                        ALTER TABLE harness_runs
                        ADD COLUMN summary_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_run_events (
                        run_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        event_json TEXT NOT NULL,
                        PRIMARY KEY(run_id, sequence)
                    )
                    """
                )
                await self._migrate_legacy_rows(db)
                await db.commit()
            self._initialized = True

    async def _migrate_legacy_rows(
        self,
        db: aiosqlite.Connection,
    ) -> None:
        async with db.execute(
            """
            SELECT run_id, state_json
            FROM harness_runs
            WHERE summary_json = '{}'
            """
        ) as cursor:
            rows = await cursor.fetchall()
        for run_id, state_json in rows:
            async with db.execute(
                """
                SELECT event_json
                FROM harness_run_events
                WHERE run_id = ?
                ORDER BY sequence
                """,
                (run_id,),
            ) as cursor:
                normalized_events = [
                    row[0] for row in await cursor.fetchall()
                ]
            run = _snapshot_from_parts(state_json, normalized_events)
            if not normalized_events:
                await db.executemany(
                    """
                    INSERT OR IGNORE INTO harness_run_events(
                        run_id, sequence, event_json
                    )
                    VALUES (?, ?, ?)
                    """,
                    [
                        (run_id, event.sequence, event.model_dump_json())
                        for event in run.events
                    ],
                )
            await db.execute(
                """
                UPDATE harness_runs
                SET state_json = ?, summary_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    _state_json(run),
                    _summary_json(run),
                    run.updated_at.isoformat(),
                    run_id,
                ),
            )

    async def save(self, run: RunSnapshot) -> None:
        await self._initialize()
        async with self._connection() as db:
            # Read the durable cursor and append its suffix under one database
            # write reservation. Parallel verifier/controller tasks use
            # separate connections; without BEGIN IMMEDIATE both can observe
            # the same cursor and attempt the same next sequence.
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(sequence), 0)
                FROM harness_run_events
                WHERE run_id = ?
                """,
                (run.run_id,),
            ) as cursor:
                durable_count, durable_cursor = await cursor.fetchone()
            if durable_count != durable_cursor:
                raise RunHistoryConflictError(
                    "durable event history is not contiguous"
                )
            durable_last_json = None
            if durable_count:
                async with db.execute(
                    """
                    SELECT event_json
                    FROM harness_run_events
                    WHERE run_id = ? AND sequence = ?
                    """,
                    (run.run_id, durable_cursor),
                ) as cursor:
                    last_row = await cursor.fetchone()
                durable_last_json = last_row[0] if last_row else None
            new_event_jsons = _new_event_jsons(
                durable_count,
                durable_last_json,
                run.events,
                event_cursor=run.event_cursor,
            )
            await db.execute(
                """
                INSERT INTO harness_runs(
                    run_id, state_json, summary_json, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    summary_json=excluded.summary_json,
                    updated_at=excluded.updated_at
                """,
                (
                    run.run_id,
                    _state_json(run),
                    _summary_json(run),
                    run.updated_at.isoformat(),
                ),
            )
            if new_event_jsons:
                first_sequence = durable_count + 1
                await db.executemany(
                    """
                    INSERT INTO harness_run_events(
                        run_id, sequence, event_json
                    )
                    VALUES (?, ?, ?)
                    """,
                    [
                        (run.run_id, sequence, event_json)
                        for sequence, event_json in enumerate(
                            new_event_jsons,
                            start=first_sequence,
                        )
                    ],
                )
            await db.commit()

    async def get(self, run_id: str) -> RunSnapshot:
        await self._initialize()
        async with self._connection() as db:
            async with db.execute(
                "SELECT state_json FROM harness_runs WHERE run_id = ?",
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise RunNotFoundError(run_id)
            async with db.execute(
                """
                SELECT event_json
                FROM harness_run_events
                WHERE run_id = ?
                ORDER BY sequence
                """,
                (run_id,),
            ) as cursor:
                event_jsons = [
                    event_row[0] for event_row in await cursor.fetchall()
                ]
        return _snapshot_from_parts(
            row[0],
            event_jsons,
            event_cursor=len(event_jsons),
        )

    async def get_control(
        self,
        run_id: str,
        event_limit: int = CONTROL_EVENT_TAIL_LIMIT,
    ) -> RunSnapshot:
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        await self._initialize()
        async with self._connection() as db:
            async with db.execute(
                """
                SELECT state_json, summary_json
                FROM harness_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise RunNotFoundError(run_id)
            summary = RunSummary.model_validate_json(row[1])
            after = max(0, summary.event_cursor - event_limit)
            async with db.execute(
                """
                SELECT event_json
                FROM harness_run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                """,
                (run_id, after),
            ) as cursor:
                event_jsons = [
                    event_row[0] for event_row in await cursor.fetchall()
                ]
        return _snapshot_from_parts(
            row[0],
            event_jsons,
            event_cursor=summary.event_cursor,
        )

    async def get_state(self, run_id: str) -> RunSnapshot:
        await self._initialize()
        async with self._connection() as db:
            async with db.execute(
                """
                SELECT state_json, summary_json
                FROM harness_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        summary = RunSummary.model_validate_json(row[1])
        return _snapshot_from_parts(
            row[0],
            [],
            event_cursor=summary.event_cursor,
        )

    async def get_summary(self, run_id: str) -> RunSummary:
        await self._initialize()
        async with self._connection() as db:
            async with db.execute(
                "SELECT summary_json FROM harness_runs WHERE run_id = ?",
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return RunSummary.model_validate_json(row[0])

    async def events_after(
        self,
        run_id: str,
        after: int,
        limit: int,
    ) -> RunEventPage:
        _, page = await self.updates_after(run_id, after, limit)
        return page

    async def events_matching(
        self,
        run_id: str,
        kinds: frozenset[str],
        limit: int,
    ) -> RunEventPage:
        if not kinds:
            raise ValueError("kinds must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        await self._initialize()
        ordered_kinds = sorted(kinds)
        placeholders = ",".join("?" for _ in ordered_kinds)
        async with self._connection() as db:
            async with db.execute(
                "SELECT summary_json FROM harness_runs WHERE run_id = ?",
                (run_id,),
            ) as cursor:
                summary_row = await cursor.fetchone()
            if summary_row is None:
                raise RunNotFoundError(run_id)
            summary = RunSummary.model_validate_json(summary_row[0])
            async with db.execute(
                f"""
                SELECT event_json
                FROM harness_run_events
                WHERE run_id = ?
                  AND json_extract(event_json, '$.kind') IN ({placeholders})
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, *ordered_kinds, limit + 1),
            ) as cursor:
                rows = await cursor.fetchall()
        return RunEventPage(
            events=[
                HarnessEvent.model_validate_json(row[0])
                for row in rows[:limit]
            ],
            latest_cursor=summary.event_cursor,
            has_more=len(rows) > limit,
        )

    async def updates_after(
        self,
        run_id: str,
        after: int,
        limit: int,
    ) -> tuple[RunSummary, RunEventPage]:
        await self._initialize()
        async with self._connection() as db:
            async with db.execute(
                "SELECT summary_json FROM harness_runs WHERE run_id = ?",
                (run_id,),
            ) as cursor:
                summary_row = await cursor.fetchone()
            if summary_row is None:
                raise RunNotFoundError(run_id)
            summary = RunSummary.model_validate_json(summary_row[0])
            async with db.execute(
                """
                SELECT event_json
                FROM harness_run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, after, limit + 1),
            ) as cursor:
                rows = await cursor.fetchall()
        return summary, RunEventPage(
            events=[
                HarnessEvent.model_validate_json(row[0])
                for row in rows[:limit]
            ],
            latest_cursor=summary.event_cursor,
            has_more=len(rows) > limit,
        )

    async def list(self, limit: int = 100) -> list[RunSnapshot]:
        summaries = await self.list_summaries(limit=limit)
        return [await self.get(summary.run_id) for summary in summaries]

    async def list_summaries(self, limit: int = 100) -> list[RunSummary]:
        await self._initialize()
        async with self._connection() as db:
            async with db.execute(
                """
                SELECT summary_json
                FROM harness_runs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            RunSummary.model_validate_json(row[0])
            for row in rows
        ]
