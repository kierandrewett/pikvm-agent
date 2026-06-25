"""Checkpointer construction.

A SQLite-backed checkpointer persists graph state after every node, so a session
survives a daemon restart and can resume from an approval interrupt. Falls back
to an in-memory saver when no path is given (tests / ephemeral runs).
"""

from __future__ import annotations

from typing import Any


async def build_checkpointer(sqlite_path: str | None = None) -> Any:
    """Build a checkpointer. With a path, an async SQLite saver (persistent);
    otherwise an in-memory saver. The caller owns the returned saver's lifecycle
    (see :func:`close_checkpointer`)."""
    if not sqlite_path:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = await aiosqlite.connect(sqlite_path)
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver


async def close_checkpointer(saver: Any) -> None:
    """Close the saver's connection if it owns one (no-op for MemorySaver)."""
    conn = getattr(saver, "conn", None)
    if conn is not None:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            pass
