"""Worker-free POSIX stdio transport for the MCP SDK server.

The MCP SDK's server transport wraps ``sys.stdin`` as an asynchronous file,
which delegates pipe reads to a worker thread. Some constrained launchers do
not service that worker even though their subprocess pipes are otherwise
usable. This adapter keeps MCP parsing and session handling in the SDK while
waiting on the POSIX file descriptors directly.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import anyio
import anyio.lowlevel
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
import mcp.types as types
from mcp.shared.message import SessionMessage


async def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        await anyio.wait_writable(fd)
        written = os.write(fd, view)
        if written <= 0:
            raise BrokenPipeError("MCP stdout closed")
        view = view[written:]


@asynccontextmanager
async def _posix_stdio_server() -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Expose newline-delimited MCP messages without an async-file worker."""

    if os.name != "posix":  # pragma: no cover - selected only on POSIX
        raise RuntimeError("worker-free stdio is available only on POSIX")

    stdin_fd = sys.stdin.buffer.fileno()
    stdout_fd = sys.stdout.buffer.fileno()
    read_sender, read_stream = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    write_stream, write_receiver = anyio.create_memory_object_stream[
        SessionMessage
    ](0)

    async def stdin_reader() -> None:
        buffer = b""
        try:
            async with read_sender:
                while True:
                    await anyio.wait_readable(stdin_fd)
                    chunk = os.read(stdin_fd, 65_536)
                    if not chunk:
                        if buffer:
                            await _send_line(buffer)
                        break
                    lines = (buffer + chunk).split(b"\n")
                    buffer = lines.pop()
                    for line in lines:
                        if line:
                            await _send_line(line)
        except (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
        ):  # pragma: no cover - peer closed during shutdown
            await anyio.lowlevel.checkpoint()

    async def _send_line(line: bytes) -> None:
        try:
            message = types.JSONRPCMessage.model_validate_json(line)
        except Exception as exc:
            await read_sender.send(exc)
            return
        await read_sender.send(SessionMessage(message))

    async def stdout_writer() -> None:
        try:
            async with write_receiver:
                async for session_message in write_receiver:
                    payload = (
                        session_message.message.model_dump_json(
                            by_alias=True,
                            exclude_none=True,
                        ).encode("utf-8")
                        + b"\n"
                    )
                    await _write_all(stdout_fd, payload)
        except (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            BrokenPipeError,
        ):  # pragma: no cover - peer closed during shutdown
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdin_reader)
        task_group.start_soon(stdout_writer)
        yield read_stream, write_stream


def run_fastmcp_stdio(server: Any) -> None:
    """Run FastMCP with worker-free POSIX pipes and SDK protocol handling."""

    if os.name != "posix":  # pragma: no cover - platform fallback
        server.run()
        return

    async def run() -> None:
        async with _posix_stdio_server() as (reader, writer):
            sdk_server = server._mcp_server
            await sdk_server.run(
                reader,
                writer,
                sdk_server.create_initialization_options(),
            )

    anyio.run(run)
