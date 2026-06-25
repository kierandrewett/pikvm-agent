"""Configuration loading.

Shape follows ``docs/PLAN.md`` → *Config shape*. A YAML file provides structure;
secrets stay in the environment (the YAML only names the env vars to read). The
default config runs against PiKVM's built-in OCR with OmniParser/OpenRouter
disabled, so the daemon starts with nothing but PiKVM credentials.

Resolution order for the config file:
    1. explicit path argument
    2. $PIKVM_AGENT_CONFIG
    3. ./config.yaml in the current working directory
    4. built-in defaults (no file needed)
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


class DaemonConfig(BaseModel):
    listen: str = "127.0.0.1:8765"
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
    mode: str = "external"  # external | managed_child_process
    base_url: str = "http://127.0.0.1:8000"
    health_url: str = "http://127.0.0.1:8000/probe"
    timeout_s: float = 20.0
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None


class OcrConfig(BaseModel):
    # "pikvm" = built-in tesseract over the snapshot endpoint (zero local deps,
    # the default). "paddleocr" = local PP-OCRv5 (needs the [vision] extra).
    provider: str = "pikvm"
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


def _find_config_file(path: str | os.PathLike[str] | None) -> Path | None:
    if path:
        return Path(path).expanduser()
    env = os.environ.get("PIKVM_AGENT_CONFIG")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd() / "config.yaml"
    if cwd.exists():
        return cwd
    return None


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load configuration, applying a YAML file over the built-in defaults."""
    cfg_path = _find_config_file(path)
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
        return AppConfig.model_validate(data)
    except Exception as exc:  # pragma: no cover - pydantic message passthrough
        raise ConfigError(f"invalid configuration: {exc}") from exc
