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


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Make config resolution hermetic: no ambient XDG/cwd config or env override
    leaks into a test (so the user's real ~/.config/pikvm-agent/config.yaml never
    changes test outcomes). Tests that want a config pass an explicit path."""
    import pikvm_agent.config as cfg

    monkeypatch.setattr(cfg, "DEFAULT_CONFIG_PATH", tmp_path / "no-config.yaml")
    monkeypatch.delenv("PIKVM_AGENT_CONFIG", raising=False)
    for var in ("PIKVM_BASE_URL", "PIKVM_VERIFY_TLS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    return AppConfig(
        daemon=DaemonConfig(
            session_dir=str(tmp_path / "sessions"),
            sqlite_path=str(tmp_path / "state.sqlite3"),
            debug_log_path=str(tmp_path / "debug.jsonl"),  # don't write the real log in tests
        )
    )


@pytest_asyncio.fixture
async def runtime(app_config: AppConfig):
    rt = await Runtime.from_config(app_config)
    try:
        yield rt
    finally:
        await rt.aclose()
