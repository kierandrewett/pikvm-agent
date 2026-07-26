from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus, RunSummary
import pikvm_agent.harness.agent_store as agent_store_module
from pikvm_agent.harness.agent_store import (
    InMemoryRunStore,
    RunHistoryConflictError,
    SqliteRunStore,
    _snapshot_from_parts,
    _state_json,
)


class _AsyncSqliteCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.cursor = cursor

    def __await__(self):
        async def ready() -> _AsyncSqliteCursor:
            return self

        return ready().__await__()

    async def __aenter__(self) -> _AsyncSqliteCursor:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.cursor.close()

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.cursor.fetchone()

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.cursor.fetchall()


class _AsyncSqliteConnection:
    """Exercise the production SQL without this runner's blocked thread pool."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)

    async def __aenter__(self) -> _AsyncSqliteConnection:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.connection.close()

    def execute(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> _AsyncSqliteCursor:
        return _AsyncSqliteCursor(
            self.connection.execute(sql, parameters)
        )

    async def executemany(
        self,
        sql: str,
        parameters: list[tuple[Any, ...]],
    ) -> None:
        self.connection.executemany(sql, parameters)

    async def commit(self) -> None:
        self.connection.commit()


def _synchronous_aiosqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_store_module.aiosqlite,
        "connect",
        lambda path: _AsyncSqliteConnection(Path(path)),
    )


def test_current_tool_activity_survives_a_bounded_event_tail() -> None:
    run = RunSnapshot(
        run_id="active-tool",
        task="Keep the current tool visible",
        status=RunStatus.RUNNING,
    )
    run.record(
        "action.attempted",
        tool="pikvm_run_burst",
        arguments={
            "session_id": "session-1",
            "actions": [{"type": "key", "keys": ["CTRL", "P"]}],
        },
        call_id="call-1",
    )
    for index in range(600):
        run.record("diagnostic.sample", index=index)

    summary = RunSummary.from_snapshot(run)

    assert run.active_activity is not None
    assert run.active_activity.kind == "tool"
    assert run.active_activity.tool == "pikvm_run_burst"
    assert run.active_activity.arguments["session_id"] == "session-1"
    assert summary.active_activity == run.active_activity
    assert run.events[-500].kind == "diagnostic.sample"

    run.record("action.completed", call_id="call-1")
    assert run.active_activity is None


def test_current_model_activity_closes_only_for_the_matching_provider_call() -> None:
    run = RunSnapshot(
        run_id="active-model",
        task="Keep the current provider visible",
        status=RunStatus.PLANNING,
    )
    run.record(
        "model.provider_started",
        role="reasoner",
        provider="claude-account",
        model="opus",
        attempt=2,
    )
    run.record(
        "model.provider_completed",
        role="controller",
        provider="codex-account",
        attempt=1,
    )
    assert run.active_activity is not None
    assert run.active_activity.provider == "claude-account"

    run.record(
        "model.provider_failed",
        role="reasoner",
        provider="claude-account",
        attempt=2,
    )
    assert run.active_activity is None


@pytest.mark.parametrize(
    "terminal_kind",
    [
        "approval.required",
        "target.identity_changed",
        "action.stale_world_refreshed",
        "action.stale_world_retry_checkpointed",
    ],
)
def test_current_tool_activity_closes_when_the_hid_request_has_ended(
    terminal_kind: str,
) -> None:
    run = RunSnapshot(
        run_id="run-terminal",
        task="End the visible request at every daemon terminal boundary",
        status=RunStatus.RUNNING,
    )
    run.record(
        "action.attempted",
        tool="pikvm_run_burst",
        attempt="not-a-number",
        arguments={"actions": [{"type": "click", "x": 10, "y": 20}]},
    )

    assert run.active_activity is not None
    assert run.active_activity.attempt is None

    run.record(terminal_kind)

    assert run.active_activity is None


@pytest.mark.asyncio
async def test_run_store_keeps_summary_reads_event_free_and_appends_once() -> None:
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="long-run",
        task="Keep a long run cheap to inventory",
        status=RunStatus.RUNNING,
    )
    for index in range(600):
        run.record("run.tick", number=index)

    await store.save(run)
    summaries = await store.list_summaries()

    assert len(summaries) == 1
    assert summaries[0].event_count == 600
    assert summaries[0].event_cursor == 600
    assert "events" not in summaries[0].model_dump()
    assert "events" not in json.loads(_state_json(run))

    run.record("run.tick", number=600)
    await store.save(run)
    durable = await store.get(run.run_id)
    state_only = await store.get_state(run.run_id)
    page = await store.events_after(run.run_id, after=595, limit=3)

    assert len(durable.events) == 601
    assert durable.events[-1].sequence == 601
    assert durable.events[-1].data == {"number": 600}
    assert state_only.events == []
    assert [event.sequence for event in page.events] == [596, 597, 598]
    assert page.latest_cursor == 601
    assert page.has_more is True


@pytest.mark.asyncio
async def test_control_snapshot_bounds_history_and_preserves_append_sequence() -> None:
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="bounded-control",
        task="Keep the live control loop bounded",
        status=RunStatus.RUNNING,
    )
    for index in range(1_200):
        run.record("run.tick", number=index)
    await store.save(run)

    control = await store.get_control(run.run_id, event_limit=64)

    assert control.event_cursor == 1_200
    assert len(control.events) == 64
    assert control.events[0].sequence == 1_137
    assert control.events[-1].sequence == 1_200

    control.record("run.tick", number=1_200)
    await store.save(control)
    durable = await store.get(run.run_id)

    assert durable.event_cursor == 1_201
    assert len(durable.events) == 1_201
    assert durable.events[-1].sequence == 1_201
    assert durable.events[-1].data == {"number": 1_200}


@pytest.mark.asyncio
async def test_run_store_rejects_truncated_or_replaced_durable_tail() -> None:
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="immutable-history",
        task="Keep event history append-only",
        status=RunStatus.RUNNING,
    )
    run.record("run.created")
    run.record("action.completed")
    await store.save(run)

    truncated = run.model_copy(deep=True)
    truncated.events.pop()
    with pytest.raises(RunHistoryConflictError, match="cannot be truncated"):
        await store.save(truncated)

    replaced = run.model_copy(deep=True)
    replaced.events[-1].kind = "action.failed"
    with pytest.raises(
        RunHistoryConflictError,
        match="cannot replace",
    ):
        await store.save(replaced)

    non_contiguous = run.model_copy(deep=True)
    non_contiguous.record("run.resumed")
    non_contiguous.events[-1].sequence = 9
    with pytest.raises(RunHistoryConflictError, match="contiguous suffix"):
        await store.save(non_contiguous)


def test_legacy_single_blob_snapshot_can_be_normalized_without_data_loss() -> None:
    run = RunSnapshot(
        run_id="legacy",
        task="Migrate the old one-row format",
        status=RunStatus.PAUSED,
    )
    run.record("run.created", source="legacy")
    run.record("run.paused", reason="operator")

    migrated = _snapshot_from_parts(run.model_dump_json(), [])

    assert migrated == run
    assert "events" not in json.loads(_state_json(migrated))


@pytest.mark.asyncio
async def test_sqlite_store_normalizes_events_and_reads_light_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _synchronous_aiosqlite(monkeypatch)
    path = tmp_path / "runs.sqlite3"
    store = SqliteRunStore(path)
    run = RunSnapshot(
        run_id="sqlite-long",
        task="Normalize append-only history",
        status=RunStatus.RUNNING,
    )
    for index in range(600):
        run.record("run.tick", number=index)

    await store.save(run)
    run.record("run.tick", number=600)
    await store.save(run)

    summary = (await store.list_summaries())[0]
    durable = await store.get(run.run_id)
    control = await store.get_control(run.run_id, event_limit=64)
    state_only = await store.get_state(run.run_id)
    page = await store.events_after(run.run_id, after=599, limit=1)
    with sqlite3.connect(path) as db:
        state_json, summary_json = db.execute(
            """
            SELECT state_json, summary_json
            FROM harness_runs
            WHERE run_id = ?
            """,
            (run.run_id,),
        ).fetchone()
        event_count = db.execute(
            """
            SELECT COUNT(*)
            FROM harness_run_events
            WHERE run_id = ?
            """,
            (run.run_id,),
        ).fetchone()[0]

    assert "events" not in json.loads(state_json)
    assert json.loads(summary_json)["event_count"] == 601
    assert event_count == 601
    assert summary.event_count == 601
    assert len(durable.events) == 601
    assert control.event_cursor == 601
    assert len(control.events) == 64
    assert control.events[0].sequence == 538
    assert control.events[-1].sequence == 601
    assert state_only.events == []
    assert [event.sequence for event in page.events] == [600]
    assert page.latest_cursor == 601
    assert page.has_more is True


@pytest.mark.asyncio
async def test_sqlite_store_migrates_the_legacy_single_blob_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy = RunSnapshot(
        run_id="legacy-sqlite",
        task="Migrate in place",
        status=RunStatus.PAUSED,
    )
    legacy.record("run.created")
    legacy.record("run.paused", reason="operator")
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE harness_runs (
                run_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO harness_runs(run_id, state_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                legacy.run_id,
                legacy.model_dump_json(),
                legacy.updated_at.isoformat(),
            ),
        )
        db.commit()
    _synchronous_aiosqlite(monkeypatch)

    store = SqliteRunStore(path)
    migrated = await store.get(legacy.run_id)

    with sqlite3.connect(path) as db:
        state_json, summary_json = db.execute(
            """
            SELECT state_json, summary_json
            FROM harness_runs
            WHERE run_id = ?
            """,
            (legacy.run_id,),
        ).fetchone()
        events = db.execute(
            """
            SELECT sequence, event_json
            FROM harness_run_events
            WHERE run_id = ?
            ORDER BY sequence
            """,
            (legacy.run_id,),
        ).fetchall()

    assert migrated == legacy
    assert "events" not in json.loads(state_json)
    assert json.loads(summary_json)["event_count"] == 2
    assert [sequence for sequence, _ in events] == [1, 2]
