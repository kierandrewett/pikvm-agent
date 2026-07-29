"""Read-only access to durable, recorded computer-use campaigns."""

from __future__ import annotations

import json
import re
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


class ShowcaseRepository:
    """Validate campaign JSON and media paths before exposing them to the UI."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> list[ShowcaseCampaign]:
        if not self.root.is_dir():
            return []
        campaigns: list[ShowcaseCampaign] = []
        for path in self.root.glob("*/campaign.json"):
            try:
                campaigns.append(self._load(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(
            campaigns,
            key=lambda campaign: campaign.updated_at,
            reverse=True,
        )

    def current(self) -> ShowcaseCampaign | None:
        campaigns = self.list()
        return campaigns[0] if campaigns else None

    def get(self, campaign_id: str) -> ShowcaseCampaign | None:
        if not SHOWCASE_ID_PATTERN.fullmatch(campaign_id):
            return None
        path = self.root / campaign_id / "campaign.json"
        if not path.is_file():
            return None
        try:
            return self._load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def media(
        self,
        campaign_id: str,
        task_id: str,
        kind: str,
    ) -> Path | None:
        campaign = self.get(campaign_id)
        if campaign is None or kind not in {"recording", "poster"}:
            return None
        task = next(
            (item for item in campaign.tasks if item.task_id == task_id),
            None,
        )
        if task is None:
            return None
        relative = getattr(task, kind)
        if not relative:
            return None
        campaign_root = (self.root / campaign_id).resolve()
        path = (campaign_root / relative).resolve()
        if campaign_root not in path.parents or not path.is_file():
            return None
        return path

    @staticmethod
    def _load(path: Path) -> ShowcaseCampaign:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return ShowcaseCampaign.model_validate(payload)
