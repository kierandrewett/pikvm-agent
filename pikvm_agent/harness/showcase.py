"""Read-only access to durable, recorded computer-use campaigns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SHOWCASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ShowcaseTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str
    category: str
    prompt: str
    status: str
    run_id: str | None = None
    recording: str | None = None
    poster: str | None = None


class ShowcaseCampaign(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    campaign_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    source_campaign_id: str | None = None
    title: str
    status: str
    total: int = Field(ge=1)
    completed: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    current_task_id: str | None = None
    current_run_id: str | None = None
    started_at: str | None = None
    updated_at: str
    tasks: list[ShowcaseTask]


@dataclass(frozen=True)
class _ShowcaseEntry:
    campaign: ShowcaseCampaign
    root: Path


class ShowcaseRepository:
    """Validate campaign JSON and media paths before exposing them to the UI."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> list[ShowcaseCampaign]:
        return sorted(
            (entry.campaign for entry in self._entries()),
            key=lambda campaign: campaign.updated_at,
            reverse=True,
        )

    def current(self) -> ShowcaseCampaign | None:
        campaigns = self.list()
        return campaigns[0] if campaigns else None

    def get(self, campaign_id: str) -> ShowcaseCampaign | None:
        entry = self._get_entry(campaign_id)
        return entry.campaign if entry is not None else None

    def _get_entry(self, campaign_id: str) -> _ShowcaseEntry | None:
        if not SHOWCASE_ID_PATTERN.fullmatch(campaign_id):
            return None
        return next(
            (
                entry
                for entry in self._entries()
                if entry.campaign.campaign_id == campaign_id
            ),
            None,
        )

    def media(
        self,
        campaign_id: str,
        task_id: str,
        kind: str,
    ) -> Path | None:
        entry = self._get_entry(campaign_id)
        if entry is None or kind not in {"recording", "poster"}:
            return None
        task = next(
            (
                item
                for item in entry.campaign.tasks
                if item.task_id == task_id
            ),
            None,
        )
        if task is None:
            return None
        relative = getattr(task, kind)
        if not relative:
            return None
        campaign_root = entry.root.resolve()
        path = (campaign_root / relative).resolve()
        if campaign_root not in path.parents or not path.is_file():
            return None
        return path

    def _entries(self) -> list[_ShowcaseEntry]:
        if not self.root.is_dir():
            return []
        repository_root = self.root.resolve()
        entries: list[_ShowcaseEntry] = []
        public_ids: set[str] = set()
        for unresolved_path in sorted(self.root.rglob("campaign.json")):
            try:
                path = unresolved_path.resolve()
                campaign_root = path.parent
                relative_root = campaign_root.relative_to(repository_root)
                if not relative_root.parts:
                    continue
                campaign = self._load(path)
                source_campaign_id = campaign.campaign_id
                public_id = self._public_id(
                    relative_root,
                    source_campaign_id,
                    public_ids,
                )
                public_ids.add(public_id)
                entries.append(
                    _ShowcaseEntry(
                        campaign=campaign.model_copy(
                            update={
                                "campaign_id": public_id,
                                "source_campaign_id": (
                                    source_campaign_id
                                    if public_id != source_campaign_id
                                    else campaign.source_campaign_id
                                ),
                            },
                        ),
                        root=campaign_root,
                    ),
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return entries

    @staticmethod
    def _public_id(
        relative_root: Path,
        source_campaign_id: str,
        used: set[str],
    ) -> str:
        outer_directory = relative_root.parts[0]
        candidate = (
            source_campaign_id
            if len(relative_root.parts) == 1
            else outer_directory
        )
        if not SHOWCASE_ID_PATTERN.fullmatch(candidate):
            candidate = "showcase"
        if candidate not in used:
            return candidate
        digest = sha256(relative_root.as_posix().encode()).hexdigest()[:8]
        return f"{candidate[:54]}-{digest}"

    @staticmethod
    def _load(path: Path) -> ShowcaseCampaign:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return ShowcaseCampaign.model_validate(payload)
