"""Pinned task discovery for stateful public desktop benchmarks.

Execution remains owned by each upstream suite: reset/setup and evaluation are
not reimplemented here. This module only validates and normalizes their public
task manifests so the harness can select tasks without importing either
project's dependency graph.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


DesktopSuiteName = Literal["osworld-verified", "windows-agent-arena"]


class PublicDesktopTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: DesktopSuiteName
    task_id: str
    declared_id: str
    domain: str
    instruction: str
    config_path: Path


class PublicDesktopSuiteInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    suite: DesktopSuiteName
    revision: str
    split_path: Path
    tasks_discovered: int
    domains: dict[str, int]
    integrity_warnings: list[str]
    tasks: list[PublicDesktopTask]


def verify_checkout_revision(repo: Path, expected: str) -> str:
    """Return the checkout SHA and refuse evidence labelled with another revision."""
    completed = subprocess.run(
        ["git", "-C", str(repo.expanduser().resolve()), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    actual = completed.stdout.strip()
    if actual != expected:
        raise ValueError(
            f"checkout revision is {actual}, but evidence requested {expected}"
        )
    return actual


def _evaluation_root(suite: DesktopSuiteName, repo: Path) -> Path:
    if suite == "osworld-verified":
        return repo / "evaluation_examples"
    return (
        repo
        / "src"
        / "win-arena-container"
        / "client"
        / "evaluation_examples_windows"
    )


def discover_desktop_suite(
    suite: DesktopSuiteName,
    repo: Path,
    *,
    revision: str,
    split: str = "test_all.json",
) -> PublicDesktopSuiteInventory:
    """Read every task in an official split and fail if any config is absent."""
    repo = repo.expanduser().resolve()
    evaluation_root = _evaluation_root(suite, repo)
    split_path = evaluation_root / split
    raw = json.loads(split_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{split_path} must contain a non-empty domain mapping")

    tasks: list[PublicDesktopTask] = []
    integrity_warnings: list[str] = []
    for domain, identifiers in raw.items():
        if not isinstance(domain, str) or not isinstance(identifiers, list):
            raise ValueError(f"invalid domain entry in {split_path}: {domain!r}")
        for task_id in identifiers:
            task_id = str(task_id)
            config_path = evaluation_root / "examples" / domain / f"{task_id}.json"
            if not config_path.is_file():
                raise FileNotFoundError(
                    f"official task config is missing for {domain}/{task_id}: "
                    f"{config_path}"
                )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            instruction = config.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                raise ValueError(f"{config_path} has no non-empty instruction")
            declared_id = str(config.get("id") or task_id)
            if declared_id != task_id:
                integrity_warnings.append(
                    f"{domain}/{task_id} declares id {declared_id!r}"
                )
            tasks.append(
                PublicDesktopTask(
                    suite=suite,
                    task_id=task_id,
                    declared_id=declared_id,
                    domain=domain,
                    instruction=instruction,
                    config_path=config_path,
                )
            )

    counts = Counter(task.domain for task in tasks)
    return PublicDesktopSuiteInventory(
        suite=suite,
        revision=revision,
        split_path=split_path,
        tasks_discovered=len(tasks),
        domains=dict(sorted(counts.items())),
        integrity_warnings=integrity_warnings,
        tasks=tasks,
    )
