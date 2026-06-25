"""Configuration loading.

Shape follows ``docs/PLAN.md`` → *Config shape*. A YAML file provides structure;
secrets stay in the environment (the YAML only names the env vars to read). The
default config runs against PiKVM's built-in OCR with OmniParser/OpenRouter
disabled, so the daemon starts with nothing but PiKVM credentials.

Resolution order for the config file:
    1. explicit path argument
    2. $PIKVM_AGENT_CONFIG
    3. $XDG_CONFIG_HOME/pikvm-agent/config.yaml  (default ~/.config/pikvm-agent/config.yaml)
    4. ./config.yaml in the current working directory (legacy)
    5. built-in defaults (no file needed)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from pikvm_agent.core.errors import ConfigError

_DATA_HOME = Path(
    os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
) / "pikvm-agent"

_CONFIG_HOME = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "pikvm-agent"

DEFAULT_CONFIG_PATH = _CONFIG_HOME / "config.yaml"
"""Where the active config lives (XDG). The repo only ships config.example.yaml."""


class DaemonConfig(BaseModel):
    listen: str = "127.0.0.1:47615"
    session_dir: str = str(_DATA_HOME / "sessions")
    sqlite_path: str = str(_DATA_HOME / "state.sqlite3")

    @property
    def host(self) -> str:
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])


class PikvmConfig(BaseModel):
    base_url: str = "https://pikvm.local"
    verify_tls: bool = False
    username_env: str = "PIKVM_USER"
    password_env: str = "PIKVM_PASSWORD"
    # Optional explicit auth token cookie env (alternative to user/pass).
    token_env: str = "PIKVM_TOKEN"
    layout: str = "us"  # us | uk; refined live from the Pi's keymap

    @property
    def username(self) -> str | None:
        return os.environ.get(self.username_env) or None

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env) or None

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None


class OmniParserConfig(BaseModel):
    enabled: bool = False
    # When required, the runtime will NOT silently fall back to OCR-only element
    # evidence: a parse failure raises loudly (OmniParser is the primary
    # perception, not a nice-to-have). The desktop default config sets this True.
    required: bool = False
    mode: str = "external"  # external | managed_child_process
    base_url: str = "http://127.0.0.1:47625"
    health_url: str = "http://127.0.0.1:47625/probe"
    timeout_s: float = 20.0
    # How long the daemon blocks waiting for OmniParser to become healthy on boot
    # before continuing. It keeps loading asynchronously after this — the first GPU
    # boot (model load + kernel compile) can take minutes, so we don't block on it.
    startup_wait_s: float = 8.0
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None


class OcrConfig(BaseModel):
    # Box-capable OCR for screen parsing (grounding needs per-word boxes):
    #   "tesseract"  = system tesseract CLI on the saved frame (default, zero
    #                  Python deps; falls back to live PiKVM OCR if absent).
    #   "paddleocr"  = local PP-OCRv5 (needs the [vision] extra).
    #   "pikvm"      = PiKVM's built-in tesseract over the live snapshot
    #                  (zero local cost, but text-only — no boxes).
    provider: str = "tesseract"
    lang: str = "en"
    device: str | None = None  # "cpu" | "gpu" | None
    disable_doc_orientation: bool = True
    disable_doc_unwarping: bool = True
    disable_textline_orientation: bool = True


class OperatorLane(BaseModel):
    model: str


class OperatorConfig(BaseModel):
    # "fake" = deterministic stub (no network; default until a key is present).
    provider: str = "fake"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    referer: str = "https://github.com/kierandrewett/pikvm-agent"
    title: str = "PiKVM Agent"
    lanes: dict[str, OperatorLane] = Field(
        default_factory=lambda: {
            "cheap": OperatorLane(model="qwen/qwen3-vl-8b-instruct"),
            "default": OperatorLane(model="qwen/qwen3-vl-32b-instruct"),
            "hard": OperatorLane(model="qwen/qwen3-vl-235b-a22b-thinking"),
        }
    )

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


class PolicyConfig(BaseModel):
    default_profile: str = "read_only_diagnostics"
    require_human_for: list[str] = Field(
        default_factory=lambda: [
            "communication_send",
            "credential_entry",
            "sensitive_data_transmit",
            "account_or_permission_change",
            "software_installation",
            "system_setting_change",
            "power_or_firmware",
            "disk_or_partition",
            "financial_or_purchase",
            "legal_or_consent",
            "terminal_mutating",
            "sudo",
            "delete",
            "local_file_edit",
            "file_external_upload",
        ]
    )
    # Blocked outright unless explicitly named in task scope.
    always_block: list[str] = Field(
        default_factory=lambda: [
            "format_disk",
            "partition_disk",
            "erase",
            "firmware_update",
            "disable_security",
            "copy_secret",
            "submit_payment",
        ]
    )


class WatchersConfig(BaseModel):
    interval_ms: int = 350
    stable_frame_count: int = 2
    global_change_threshold: float = 0.08
    # Perceptual fingerprint thresholds (see docs/PIKVM_API.md).
    fp_move: float = 0.04
    fp_settle: float = 0.015
    fp_meaningful: float = 0.05


class AppConfig(BaseModel):
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    pikvm: PikvmConfig = Field(default_factory=PikvmConfig)
    omniparser: OmniParserConfig = Field(default_factory=OmniParserConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    operator: OperatorConfig = Field(default_factory=OperatorConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    watchers: WatchersConfig = Field(default_factory=WatchersConfig)


def _load_dotenv(path: Path, *, override: bool = False) -> int:
    """Load KEY=VALUE lines from a ``.env`` file into the environment.

    By default does NOT override an already-set var (so env forwarded by the
    desktop app at spawn time wins over the file). Supports ``export KEY=...``,
    ``#`` comments, blank lines, and single/double-quoted values. Returns the
    number of vars set."""
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = val
            count += 1
    return count


def _find_config_file(path: str | os.PathLike[str] | None) -> Path | None:
    if path:
        return Path(path).expanduser()
    env = os.environ.get("PIKVM_AGENT_CONFIG")
    if env:
        return Path(env).expanduser()
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    cwd = Path.cwd() / "config.yaml"
    if cwd.exists():
        return cwd
    return None


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load configuration, applying a YAML file over the built-in defaults.

    A ``.env`` in the config folder (``~/.config/pikvm-agent/.env`` and/or the
    directory of an explicit config file) is loaded first, so PiKVM/OpenRouter
    secrets can live beside the config rather than being exported by hand.
    """
    _load_dotenv(DEFAULT_CONFIG_PATH.parent / ".env")
    cfg_path = _find_config_file(path)
    if cfg_path is not None and cfg_path.parent != DEFAULT_CONFIG_PATH.parent:
        _load_dotenv(cfg_path.parent / ".env")
    data: dict[str, Any] = {}
    if cfg_path is not None:
        if not cfg_path.exists():
            raise ConfigError(f"config file not found: {cfg_path}")
        try:
            loaded = yaml.safe_load(cfg_path.read_text()) or {}
        except yaml.YAMLError as exc:  # pragma: no cover - passthrough
            raise ConfigError(f"invalid YAML in {cfg_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"config root must be a mapping: {cfg_path}")
        data = loaded
    try:
        cfg = AppConfig.model_validate(data)
    except Exception as exc:  # pragma: no cover - pydantic message passthrough
        raise ConfigError(f"invalid configuration: {exc}") from exc

    # Env overrides for the few values the desktop app forwards at spawn time, so
    # it can inject the live PiKVM origin without writing a config file.
    if os.environ.get("PIKVM_BASE_URL"):
        cfg.pikvm.base_url = os.environ["PIKVM_BASE_URL"]
    if os.environ.get("PIKVM_VERIFY_TLS"):
        cfg.pikvm.verify_tls = os.environ["PIKVM_VERIFY_TLS"].lower() in ("1", "true", "yes", "on")
    return cfg
