"""Desktop-integration surface: XDG config, .env loading, env overrides,
OmniParser required + managed launcher, config CLI."""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.config import DEFAULT_CONFIG_PATH, OmniParserConfig, load_config
from pikvm_agent.core.errors import BackendError
from pikvm_agent.core.models import ElementMap
from pikvm_agent.vision.omniparser_client import OmniParserProvider
from pikvm_agent.vision.omniparser_manager import OmniParserManager


# ---- config: XDG, env overrides, .env ------------------------------------- #

def test_default_config_path_is_xdg() -> None:
    assert DEFAULT_CONFIG_PATH.name == "config.yaml"
    assert DEFAULT_CONFIG_PATH.parent.name == "pikvm-agent"
    assert ".config" in str(DEFAULT_CONFIG_PATH) or "XDG_CONFIG_HOME" in str(DEFAULT_CONFIG_PATH)


def test_pikvm_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIKVM_BASE_URL", "https://mypi.lan:8443")
    monkeypatch.setenv("PIKVM_VERIFY_TLS", "true")
    c = load_config()
    assert c.pikvm.base_url == "https://mypi.lan:8443"
    assert c.pikvm.verify_tls is True


def test_dotenv_loaded_from_config_folder(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # _isolate_config points DEFAULT_CONFIG_PATH at tmp_path/no-config.yaml.
    monkeypatch.delenv("PIKVM_USER", raising=False)
    (tmp_path / ".env").write_text("PIKVM_USER=envuser\nOPENROUTER_API_KEY=or-key-123\n")
    c = load_config()
    assert c.pikvm.username == "envuser"
    assert c.operator.api_key == "or-key-123"


def test_dotenv_does_not_override_existing_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PIKVM_USER", "real-from-env")
    (tmp_path / ".env").write_text("PIKVM_USER=should-not-win\n")
    c = load_config()
    assert c.pikvm.username == "real-from-env"  # process env wins over .env


# ---- OmniParser: required raises, optional degrades ----------------------- #

class _BoomClient:
    base_url = "http://127.0.0.1:9"

    async def parse_image(self, _p):
        raise RuntimeError("server down")


async def test_omniparser_required_raises(tmp_path) -> None:
    img = tmp_path / "f.png"
    img.write_bytes(b"\xff\xd8\xff")  # not parsed (client raises first)
    with pytest.raises(BackendError):
        await OmniParserProvider(_BoomClient(), required=True).parse_elements(img, 1, 1)


async def test_omniparser_optional_degrades(tmp_path) -> None:
    img = tmp_path / "f.png"
    img.write_bytes(b"\xff\xd8\xff")
    em = await OmniParserProvider(_BoomClient(), required=False).parse_elements(img, 1, 1)
    assert isinstance(em, ElementMap) and em.elements == []


# ---- OmniParser manager lifecycle ----------------------------------------- #

async def test_manager_healthy_skips_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = OmniParserManager(OmniParserConfig(enabled=True, mode="managed_child_process"))

    async def _healthy() -> bool:
        return True

    monkeypatch.setattr(mgr.client, "health", _healthy)
    assert await mgr.ensure_running(wait_s=0.1) is True
    assert mgr._proc is None  # already up ⇒ never spawned
    assert mgr.spawned_child is False  # adopted an existing server, didn't launch one


async def test_manager_spawns_and_stops() -> None:
    cfg = OmniParserConfig(
        enabled=True, required=False, mode="managed_child_process",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        base_url="http://127.0.0.1:59599", health_url="http://127.0.0.1:59599/probe",
    )
    mgr = OmniParserManager(cfg)
    up = await mgr.ensure_running(wait_s=0.3, poll_s=0.1)  # nothing serving ⇒ stays down
    assert up is False
    assert mgr._proc is not None and mgr._proc.returncode is None  # but the child is running
    assert mgr.spawned_child is True  # we launched it (it's just still booting)
    await mgr.stop()
    assert mgr._proc is None
    assert mgr.spawned_child is False  # stopped ⇒ no longer ours


async def test_manager_external_mode_never_spawns() -> None:
    # External mode = the server runs elsewhere; the manager only health-checks it.
    cfg = OmniParserConfig(
        enabled=True, mode="external",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],  # ignored in external mode
        base_url="http://127.0.0.1:59600", health_url="http://127.0.0.1:59600/probe",
    )
    mgr = OmniParserManager(cfg)
    up = await mgr.ensure_running(wait_s=0.2, poll_s=0.1)
    assert up is False
    assert mgr.spawned_child is False  # external ⇒ never spawns, even when down


# ---- config CLI ----------------------------------------------------------- #

def test_config_init_scaffolds_config_and_env() -> None:
    # _isolate_config repoints DEFAULT_CONFIG_PATH into tmp; read the *patched*
    # value via the module (the top-level import is bound to the real path).
    import pikvm_agent.config as cfg

    result = CliRunner().invoke(app, ["config-init"])
    assert result.exit_code == 0, result.output
    assert cfg.DEFAULT_CONFIG_PATH.exists()
    env_file = cfg.DEFAULT_CONFIG_PATH.parent / ".env"
    assert env_file.exists()
    assert "OPENROUTER_API_KEY" in env_file.read_text()


def test_config_path_runs() -> None:
    result = CliRunner().invoke(app, ["config-path"])
    assert result.exit_code == 0 and "default config" in result.output
