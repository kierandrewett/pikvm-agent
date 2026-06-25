"""Shared test fixtures.

All tests run against the FakeBackend (no PiKVM hardware). Each test gets an
isolated session dir + SQLite file under tmp_path.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from pikvm_agent.config import AppConfig, DaemonConfig
from pikvm_agent.runtime import Runtime


@pytest.fixture(autouse=True)
def _force_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIKVM_AGENT_FAKE", "1")


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    return AppConfig(
        daemon=DaemonConfig(
            session_dir=str(tmp_path / "sessions"),
            sqlite_path=str(tmp_path / "state.sqlite3"),
        )
    )


@pytest_asyncio.fixture
async def runtime(app_config: AppConfig):
    rt = await Runtime.from_config(app_config)
    try:
        yield rt
    finally:
        await rt.aclose()
