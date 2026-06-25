"""Manage the OmniParser server as a child process of the daemon.

When ``omniparser.mode == "managed_child_process"`` the daemon starts the
configured ``command`` (in ``cwd``) on boot and waits briefly for its health
endpoint, then kills it on shutdown. When the server runs externally (or on
another host) this is a no-op beyond the health check.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from pikvm_agent.config import OmniParserConfig
from pikvm_agent.vision.omniparser_client import OmniParserClient

log = logging.getLogger("pikvm_agent.vision.omniparser_manager")


class OmniParserManager:
    def __init__(self, config: OmniParserConfig) -> None:
        self.config = config
        self.client = OmniParserClient(
            base_url=config.base_url, health_url=config.health_url, timeout_s=config.timeout_s
        )
        self._proc: Any | None = None

    async def healthy(self) -> bool:
        return await self.client.health()

    async def _spawn(self) -> None:
        if self._proc is not None or self.config.mode != "managed_child_process":
            return
        if not self.config.command:
            log.warning("OmniParser managed mode set but no command configured")
            return
        log.info("starting OmniParser: %s (cwd=%s)", " ".join(self.config.command), self.config.cwd)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.config.command,
                cwd=self.config.cwd or None,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("failed to spawn OmniParser: %s", exc)

    async def ensure_running(self, wait_s: float = 20.0, poll_s: float = 1.0) -> bool:
        """Return True once OmniParser is healthy. If managed, spawn it first and
        poll its health up to ``wait_s`` (it loads models on boot)."""
        if await self.healthy():
            return True
        await self._spawn()
        deadline = asyncio.get_event_loop().time() + wait_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_s)
            if await self.healthy():
                return True
        return await self.healthy()

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
