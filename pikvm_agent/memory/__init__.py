"""Post-session Atlas memory loop (Phase 8).

Turns a session's trace into a SAFE, durable Atlas memory-update *proposal*.
This package is pure logic: it never touches the network and never writes to
Atlas itself. Claude/Codex writes the proposal to Atlas via the atlas MCP tools.

The plan forbids leaking secrets, raw screenshots, credentials, private message
bodies, and API keys into Atlas; redaction here is the enforcement point.
"""

from __future__ import annotations

from pikvm_agent.memory.atlas_export import (
    MemoryUpdate,
    REDACT_KEYS,
    build_memory_update,
    memory_slug,
)

__all__ = [
    "MemoryUpdate",
    "REDACT_KEYS",
    "build_memory_update",
    "memory_slug",
]
