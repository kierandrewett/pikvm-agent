"""Measure long-run checkpoint, inventory, and event-tail storage costs."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import resource
import sqlite3
import statistics
import tempfile
import time

from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.agent_store import (
    InMemoryRunStore,
    SqliteRunStore,
    _state_json,
    _summary_json,
)


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def latency(values: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


async def measure(event_count: int, repetitions: int) -> dict[str, object]:
    run = RunSnapshot(
        run_id="storage-benchmark",
        task="Measure normalized long-run storage",
        status=RunStatus.RUNNING,
    )
    for index in range(event_count):
        run.record("run.tick", number=index)
    store = InMemoryRunStore()

    started = time.perf_counter()
    await store.save(run)
    initial_save_ms = (time.perf_counter() - started) * 1_000

    append_latencies = []
    summary_latencies = []
    tail_latencies = []
    control_latencies = []
    control_event_counts = []
    for index in range(repetitions):
        run.record("run.tick", number=event_count + index)
        started = time.perf_counter()
        await store.save(run)
        append_latencies.append((time.perf_counter() - started) * 1_000)

        started = time.perf_counter()
        await store.list_summaries(limit=100)
        summary_latencies.append((time.perf_counter() - started) * 1_000)

        started = time.perf_counter()
        await store.events_after(
            run.run_id,
            after=len(run.events) - 500,
            limit=500,
        )
        tail_latencies.append((time.perf_counter() - started) * 1_000)

        started = time.perf_counter()
        control = await store.get_control(run.run_id)
        control_latencies.append((time.perf_counter() - started) * 1_000)
        control_event_counts.append(len(control.events))

    legacy_checkpoint_bytes = len(run.model_dump_json().encode())
    normalized_checkpoint_bytes = len(_state_json(run).encode()) + len(
        _summary_json(run).encode()
    )
    latest_event_bytes = len(run.events[-1].model_dump_json().encode())
    return {
        "schema_version": 1,
        "suite": "harness-normalized-storage",
        "events_before_repetitions": event_count,
        "append_repetitions": repetitions,
        "events_after_repetitions": len(run.events),
        "initial_history_import_ms": round(initial_save_ms, 3),
        "append_checkpoint_latency": latency(append_latencies),
        "event_free_inventory_latency": latency(summary_latencies),
        "latest_500_event_page_latency": latency(tail_latencies),
        "bounded_1000_event_control_load_latency": latency(control_latencies),
        "bounded_control_event_count": {
            "min": min(control_event_counts),
            "max": max(control_event_counts),
        },
        "legacy_full_checkpoint_bytes": legacy_checkpoint_bytes,
        "normalized_state_plus_summary_bytes": normalized_checkpoint_bytes,
        "latest_event_append_bytes": latest_event_bytes,
        "checkpoint_write_reduction_ratio": round(
            legacy_checkpoint_bytes
            / max(1, normalized_checkpoint_bytes + latest_event_bytes),
            3,
        ),
        "storage_adapter": "in-memory normalized contract",
        "limitations": [
            "This isolates serialization and storage-contract costs.",
            "It does not include aiosqlite, filesystem, model, OCR, HID, or network latency.",
        ],
    }


async def measure_sqlite(
    event_count: int,
    repetitions: int,
    database: Path,
) -> dict[str, object]:
    """Exercise the production SQLite adapter on a real local filesystem."""

    if database.exists():
        raise ValueError(f"refusing to replace existing database: {database}")
    run = RunSnapshot(
        run_id="sqlite-storage-benchmark",
        task="Measure bounded control snapshots on real SQLite",
        status=RunStatus.RUNNING,
    )
    for index in range(event_count):
        run.record("run.tick", number=index)
    store = SqliteRunStore(database)

    started = time.perf_counter()
    await store.save(run)
    initial_save_ms = (time.perf_counter() - started) * 1_000

    append_latencies = []
    summary_latencies = []
    tail_latencies = []
    control_latencies = []
    control_event_counts = []
    for index in range(repetitions):
        run.record("run.tick", number=event_count + index)
        started = time.perf_counter()
        await store.save(run)
        append_latencies.append((time.perf_counter() - started) * 1_000)

        started = time.perf_counter()
        await store.list_summaries(limit=100)
        summary_latencies.append((time.perf_counter() - started) * 1_000)

        started = time.perf_counter()
        await store.events_after(
            run.run_id,
            after=max(0, run.event_cursor - 500),
            limit=500,
        )
        tail_latencies.append((time.perf_counter() - started) * 1_000)

        started = time.perf_counter()
        control = await store.get_control(run.run_id)
        control_latencies.append((time.perf_counter() - started) * 1_000)
        control_event_counts.append(len(control.events))

    started = time.perf_counter()
    replayed = await SqliteRunStore(database).get(run.run_id)
    full_replay_ms = (time.perf_counter() - started) * 1_000
    with sqlite3.connect(database) as db:
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        durable_event_rows = int(
            db.execute(
                """
                SELECT COUNT(*)
                FROM harness_run_events
                WHERE run_id = ?
                """,
                (run.run_id,),
            ).fetchone()[0]
        )
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
    expected_events = event_count + repetitions
    if durable_event_rows != expected_events:
        raise RuntimeError(
            f"expected {expected_events} durable events, found {durable_event_rows}"
        )
    if replayed.event_cursor != expected_events:
        raise RuntimeError(
            f"expected replay cursor {expected_events}, "
            f"found {replayed.event_cursor}"
        )
    return {
        "schema_version": 1,
        "suite": "harness-sqlite-storage-soak",
        "events_before_repetitions": event_count,
        "append_repetitions": repetitions,
        "events_after_repetitions": expected_events,
        "initial_history_import_ms": round(initial_save_ms, 3),
        "append_checkpoint_latency": latency(append_latencies),
        "event_free_inventory_latency": latency(summary_latencies),
        "latest_500_event_page_latency": latency(tail_latencies),
        "bounded_1000_event_control_load_latency": latency(control_latencies),
        "bounded_control_event_count": {
            "min": min(control_event_counts),
            "max": max(control_event_counts),
        },
        "full_history_replay_ms": round(full_replay_ms, 3),
        "full_history_replay_events": len(replayed.events),
        "database_bytes": database.stat().st_size,
        "database_allocated_bytes": page_size * page_count,
        "durable_event_rows": durable_event_rows,
        "sqlite_integrity_check": integrity,
        "max_resident_set_kib": resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss,
        "storage_adapter": "production SqliteRunStore on local filesystem",
        "limitations": [
            "This is a bounded single-process soak, not a multi-hour concurrency test.",
            "It excludes model, frame decode, OCR, HID, and network latency.",
            "Maximum resident set is a process peak, not per-operation allocation.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument(
        "--adapter",
        choices=("memory", "sqlite"),
        default="memory",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        help="Non-existing SQLite path; omitted uses a temporary directory.",
    )
    args = parser.parse_args()
    if args.adapter == "memory":
        report = asyncio.run(measure(args.events, args.repetitions))
    elif args.sqlite_path is not None:
        report = asyncio.run(
            measure_sqlite(args.events, args.repetitions, args.sqlite_path)
        )
    else:
        with tempfile.TemporaryDirectory(prefix="pikvm-sqlite-soak-") as tmp:
            report = asyncio.run(
                measure_sqlite(
                    args.events,
                    args.repetitions,
                    Path(tmp) / "state.sqlite3",
                )
            )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
