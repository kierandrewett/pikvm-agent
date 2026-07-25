"""Versioned messages emitted by the Windows accuracy observer."""

from __future__ import annotations

import base64
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputEvent(BaseModel):
    at_ms: int
    kind: str
    vk: int | None = None
    scan: int | None = None
    text: str | None = None
    x: int | None = None
    y: int | None = None
    button: str | None = None


class DangerousCommit(BaseModel):
    at_ms: int
    kind: str
    label: str


class ObservedFile(BaseModel):
    path: str
    content_base64: str
    error: str = ""

    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)


class OracleSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["pikvm-observer.v1"]
    sequence: int
    text: str
    events: list[InputEvent] = Field(default_factory=list)
    input_event_count: int | None = None
    key_down_vks: list[int] | None = None
    key_down_count: int | None = None
    key_down_vks_truncated: bool = False
    dangerous_commits: list[DangerousCommit] = Field(default_factory=list)
    file: ObservedFile | None = None
    foreground_title: str = ""
    foreground_executable: str = ""
    foreground_process_id: int | None = Field(default=None, ge=1)
    focused_control_class: str = Field(default="", max_length=512)
    focused_control_id: int | None = Field(default=None, ge=0)
    focus_in_foreground: bool | None = None
    guest_fingerprint: str = Field(
        default="",
        pattern=r"^(?:|guest:[0-9a-f]{16})$",
    )
    guest_session_id: int | None = Field(default=None, ge=0)
    input_desktop: str = Field(default="", max_length=512)

    @model_validator(mode="before")
    @classmethod
    def expand_visual_wire_keys(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        expanded = dict(value)
        aliases = {
            "p": "protocol",
            "s": "sequence",
            "t": "text",
            "e": "events",
            "ic": "input_event_count",
            "kv": "key_down_vks",
            "kc": "key_down_count",
            "kt": "key_down_vks_truncated",
            "dc": "dangerous_commits",
            "ft": "foreground_title",
            "fe": "foreground_executable",
            "fp": "foreground_process_id",
            "fc": "focused_control_class",
            "fi": "focused_control_id",
            "ff": "focus_in_foreground",
            "gf": "guest_fingerprint",
            "gs": "guest_session_id",
            "id": "input_desktop",
            "fl": "file",
        }
        for compact, field in aliases.items():
            if compact in expanded and field not in expanded:
                expanded[field] = expanded.pop(compact)
        return expanded

    def safe_environment(self) -> dict[str, str | int | bool | None]:
        return {
            "foreground_title": self.foreground_title,
            "foreground_executable": self.foreground_executable,
            "foreground_process_id": self.foreground_process_id,
            "focused_control_class": self.focused_control_class,
            "focused_control_id": self.focused_control_id,
            "focus_in_foreground": self.focus_in_foreground,
            "guest_fingerprint": self.guest_fingerprint or None,
            "guest_session_id": self.guest_session_id,
            "input_desktop": self.input_desktop,
        }
