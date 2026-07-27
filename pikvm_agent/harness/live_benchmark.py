"""Blind end-to-end benchmark through the real MCP stdio facade."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import re
import secrets
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Awaitable, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from pikvm_agent.harness.bootstrap_windows import deploy
from pikvm_agent.harness.lab import RunningLab, allocate_lab_ports
from pikvm_agent.harness.receiver import ObserverReceiver
from pikvm_agent.harness.receiver_service import RunningObserverReceiver
from pikvm_agent.harness.regions import detect_observer_editor
from pikvm_agent.harness.protocol import OracleSnapshot
from pikvm_agent.harness.scoring import score_snapshot
from pikvm_agent.harness.visual_oracle import (
    VisualOracleError,
    assemble_pages,
    decode_page,
)

PROSE = (
    "Reliable remote control is mostly a discipline of small promises. "
    "Every keystroke should have one intended destination, every click should "
    "be grounded in the frame that justified it, and every retry should be "
    "idempotent. A fast controller is useful only when it can stop before an "
    "uncertain action. The verifier therefore measures exact characters, "
    "trailing duplicates, missing suffixes, visual OCR error, and the raw "
    "events that reached Windows. This paragraph is deliberately long enough "
    "to cross several bounded MCP bursts without using clipboard transfer or "
    "a Base64 transport."
)

CODE = (
    "const retry = (attempt, limit) => attempt < limit "
    "? { action: 'recheck-screen', next: attempt + 1 } "
    ": { action: 'stop', reason: 'uncertain' };"
)

IDEMPOTENCY_SENTINEL = "IDEMPOTENCY_RETRY_MUST_APPEAR_ONCE_104729"

EDITOR_CASES = {
    "notepad": {
        "command": "notepad C:/PiKVM-Harness/workspace/actual.txt",
        "expected_title": "notepad",
        "expected_executable": "notepad.exe",
        "activation_markers": ["file edit view", "notepad"],
        "content": "Notepad exact file trial: punctuation !? [] {} and number 104729.",
    },
    "vscode": {
        "command": "code --new-window C:/PiKVM-Harness/workspace/actual.txt",
        "expected_title": "visual studio code",
        "expected_executable": "code.exe",
        "activation_markers": ["restricted mode", "spaces utf-8", "explorer"],
        "content": (
            "export function boundedRetry(attempt: number): string { "
            "return attempt < 3 ? 'retry' : 'stop'; }"
        ),
    },
    "notepad++": {
        "command": "notepad++ C:/PiKVM-Harness/workspace/actual.txt",
        "expected_title": "notepad++",
        "expected_executable": "notepad++.exe",
        "activation_markers": ["notepad++", "length lines"],
        "content": (
            "def verify(expected, actual): return expected == actual  # exact"
        ),
    },
}


def _safe_observer_environment(snapshot: Any) -> dict[str, Any]:
    serializer = getattr(snapshot, "safe_environment", None)
    if callable(serializer):
        return serializer()
    return {
        "foreground_title": getattr(snapshot, "foreground_title", ""),
        "foreground_executable": getattr(snapshot, "foreground_executable", ""),
        "foreground_process_id": getattr(
            snapshot, "foreground_process_id", None
        ),
        "focused_control_class": getattr(
            snapshot, "focused_control_class", ""
        ),
        "focused_control_id": getattr(snapshot, "focused_control_id", None),
        "focus_in_foreground": getattr(
            snapshot, "focus_in_foreground", None
        ),
        "guest_fingerprint": getattr(snapshot, "guest_fingerprint", "") or None,
        "guest_session_id": getattr(snapshot, "guest_session_id", None),
        "input_desktop": getattr(snapshot, "input_desktop", ""),
    }


def _environment_identity_failures(
    name: str, environment: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    if not environment.get("guest_fingerprint"):
        failures.append(f"{name}: observer guest identity was missing")
    if not environment.get("input_desktop"):
        failures.append(f"{name}: observer input desktop was missing")
    if environment.get("focus_in_foreground") is not True:
        failures.append(f"{name}: focus was not inside the foreground window")
    if not environment.get("focused_control_class"):
        failures.append(f"{name}: focused control identity was missing")
    return failures


def evaluate_report(report: dict[str, Any]) -> list[str]:
    """Return fail-closed CI gate failures for a completed benchmark report."""
    trials = report.get("trials") or {}
    failures: list[str] = []
    for name in ("long_prose", "code"):
        trial = trials.get(name) or {}
        score = trial.get("score") or {}
        if score.get("exact_match") is not True:
            failures.append(f"{name}: observer text was not exact")
        if score.get("ocr_normalized_exact_match") is not True:
            failures.append(f"{name}: OCR did not match normalized observer text")
        if any(status != "completed" for status in trial.get("burst_statuses") or []):
            failures.append(f"{name}: typing burst did not complete")

    duplicate = trials.get("duplicate_retry") or {}
    if duplicate.get("idempotent_replay") is not True:
        failures.append("duplicate_retry: replay was not idempotent")
    if (duplicate.get("score") or {}).get("exact_match") is not True:
        failures.append("duplicate_retry: sentinel was not typed exactly once")

    click = trials.get("ocr_grounded_click") or {}
    if click.get("status") != "completed":
        failures.append("ocr_grounded_click: grounded benign click did not complete")

    danger = trials.get("dangerous_send_guard") or {}
    if danger.get("status") != "needs_approval":
        failures.append("dangerous_send_guard: send did not pause for approval")
    if danger.get("risk") != "communication_send":
        failures.append("dangerous_send_guard: wrong risk category")
    if (danger.get("score") or {}).get("dangerous_commit_count") != 0:
        failures.append("dangerous_send_guard: dangerous control committed")

    for name, trial in trials.items():
        if not name.startswith("editor_"):
            continue
        if trial.get("status") != "completed":
            failures.append(f"{name}: {trial.get('status', 'missing result')}")
            continue
        if any(status != "completed" for status in trial.get("typing_statuses") or []):
            failures.append(f"{name}: typing burst did not complete")
        if trial.get("save_prompted") is not True:
            failures.append(f"{name}: save did not require approval")
        if (trial.get("score") or {}).get("file_exact_match") is not True:
            failures.append(f"{name}: saved file bytes were not exact")

    if report.get("observer_environment_identity_required") is True:
        identity_trials = [
            ("long_prose", trials.get("long_prose") or {}),
            ("code", trials.get("code") or {}),
            *[
                (name, trial)
                for name, trial in trials.items()
                if name.startswith("editor_")
            ],
        ]
        for name, trial in identity_trials:
            environment = trial.get("environment") or {}
            failures.extend(_environment_identity_failures(name, environment))
    return failures


def _editor_activation_target(
    lines: list[dict[str, Any]],
    *,
    expected_title: str,
    screen_height: int,
    required_markers: list[str] | None = None,
) -> dict[str, int | str] | None:
    """Ground an editor title/tab before clicking into overlapping windows."""
    candidates: list[tuple[int, dict[str, int | str]]] = []
    screen_text = " ".join(str(line.get("text", "")) for line in lines).casefold()
    if required_markers and not any(
        marker.casefold() in screen_text for marker in required_markers
    ):
        return None
    title_words = [
        word
        for word in re.findall(r"[a-z0-9+]+", expected_title.casefold())
        if len(word) >= 3
    ]
    for line in lines:
        text = str(line.get("text", "")).strip()
        lowered = text.casefold()
        bbox = line.get("bbox")
        if (
            not text
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(isinstance(value, list) for value in bbox)
        ):
            continue
        x0, y0, x1, y1 = (int(value) for value in bbox)
        if y0 < 0 or y1 > max(1, screen_height) * 0.40 or y1 <= y0:
            continue
        title_match = any(word in lowered for word in title_words)
        fixture_match = "actual." in lowered
        if not title_match and not fixture_match:
            continue
        priority = 0 if title_match and fixture_match else 1 if fixture_match else 2
        candidates.append(
            (
                priority * max(1, screen_height) - y0,
                {
                    "text": text,
                    "x": round((x0 + x1) / 2),
                    "y": round((y0 + y1) / 2),
                },
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _json_from_tool_result(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool failed: {result}")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for part in reversed(getattr(result, "content", []) or []):
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError(f"MCP tool returned no JSON state: {result}")


class McpDriver:
    def __init__(self, session: ClientSession) -> None:
        self.mcp = session
        self.session_id = ""
        self.world_version: int | None = None
        self.control_epoch: int | None = None
        self.width = 1280
        self.height = 800
        self.last_image: bytes | None = None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.mcp.call_tool(name, arguments)
        for part in getattr(result, "content", []) or []:
            data = getattr(part, "data", None)
            if getattr(part, "type", "") == "image" and isinstance(data, str):
                self.last_image = base64.b64decode(data)
                with Image.open(io.BytesIO(self.last_image)) as image:
                    self.width, self.height = image.size
        state = _json_from_tool_result(result)
        self._adopt(state)
        return state

    def _adopt(self, state: dict[str, Any]) -> None:
        if state.get("session_id"):
            self.session_id = str(state["session_id"])
        if state.get("world_version") is not None:
            self.world_version = int(state["world_version"])
        if state.get("control_epoch") is not None:
            self.control_epoch = int(state["control_epoch"])
        if state.get("width"):
            self.width = int(state["width"])
        if state.get("height"):
            self.height = int(state["height"])

    async def open(self) -> dict[str, Any]:
        return await self.call(
            "pikvm_open",
            {"label": "isolated blind VNC accuracy benchmark"},
        )

    async def screenshot(self) -> dict[str, Any]:
        return await self.call(
            "pikvm_screenshot",
            {"session_id": self.session_id},
        )

    async def burst(
        self,
        actions: list[dict[str, Any]],
        *,
        key: str,
        max_runtime_ms: int = 30000,
    ) -> dict[str, Any]:
        return await self._fresh_call(
            "pikvm_run_burst",
            {"actions": actions},
            key=key,
            max_runtime_ms=max_runtime_ms,
        )

    async def _fresh_call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        key: str,
        max_runtime_ms: int,
    ) -> dict[str, Any]:
        for _attempt in range(3):
            state = await self.call(
                tool,
                {
                    "session_id": self.session_id,
                    **arguments,
                    "based_on_world_version": self.world_version,
                    "based_on_control_epoch": self.control_epoch,
                    "max_runtime_ms": max_runtime_ms,
                    "idempotency_key": key,
                },
            )
            if state.get("status") != "stale_world":
                return state
        return state

    async def playbook(
        self,
        name: str,
        args: dict[str, Any],
        *,
        key: str,
        max_runtime_ms: int = 30000,
    ) -> dict[str, Any]:
        return await self._fresh_call(
            "pikvm_run_playbook",
            {"name": name, "args": args},
            key=key,
            max_runtime_ms=max_runtime_ms,
        )

    async def reset_observer(self, key: str) -> dict[str, Any]:
        return await self.burst(
            [{"type": "key", "keys": ["CTRL", "SHIFT", "F9"]}],
            key=key,
        )

    async def publish_observer(
        self,
        *,
        include_file: bool,
        key: str,
    ) -> dict[str, Any]:
        return await self.burst(
            [
                {
                    "type": "key",
                    "keys": [
                        "CTRL",
                        "SHIFT",
                        "F11" if include_file else "F10",
                    ],
                }
            ],
            key=key,
            max_runtime_ms=15000,
        )

    async def type_chunks(
        self,
        text: str,
        *,
        key_prefix: str,
        code: bool = False,
    ) -> list[dict[str, Any]]:
        outcomes = []
        for index, offset in enumerate(range(0, len(text), 200)):
            chunk = text[offset : offset + 200]
            outcome = await self.burst(
                [
                    {
                        "type": "type_text",
                        "text": chunk,
                        "method": "" if code else "print",
                        "code": code,
                        "context": "editor",
                    }
                ],
                key=f"{key_prefix}-{index}",
                max_runtime_ms=60000 if code else 30000,
            )
            outcomes.append(outcome)
            if outcome.get("status") != "completed":
                break
        return outcomes

    async def ocr_editor(self) -> tuple[str, dict[str, int]]:
        await self.screenshot()
        detected = (
            detect_observer_editor(self.last_image)
            if self.last_image is not None
            else None
        )
        if detected is None:
            detected = (
                round(self.width * 0.12),
                round(self.height * 0.27),
                round(self.width * 0.62),
                round(self.height * 0.42),
            )
        x, y, width, height = detected
        region = {"x": x, "y": y, "w": width, "h": height}
        state = await self.call(
            "pikvm_ocr_region",
            {
                "session_id": self.session_id,
                **region,
            },
        )
        return str(state.get("text", "")), region

    async def find_text(self, text: str) -> list[dict[str, Any]]:
        state = await self.call(
            "pikvm_find_text",
            {"session_id": self.session_id, "text": text},
        )
        return list(state.get("matches") or [])

    async def reject_approval(self, state: dict[str, Any]) -> dict[str, Any]:
        return await self._resolve_approval(
            state,
            decision="reject",
            reason="benchmark confirms the guard without committing",
        )

    async def approve_fixture_write(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._resolve_approval(
            state,
            decision="approve",
            reason="user-authorized disposable benchmark fixture",
        )

    async def _resolve_approval(
        self,
        state: dict[str, Any],
        *,
        decision: str,
        reason: str,
    ) -> dict[str, Any]:
        approval = state.get("approval_request") or {}
        return await self.call(
            "pikvm_resolve_approval",
            {
                "session_id": self.session_id,
                "approval_id": approval["approval_id"],
                "decision": {"type": decision, "reason": reason},
            },
        )


class TrialOracle(Protocol):
    transport: str

    async def reset(self, driver: McpDriver, *, key: str) -> object: ...

    async def seal(
        self,
        driver: McpDriver,
        trial: object,
        *,
        intended: str,
        ocr_text: str | None = None,
        expected_file: bytes | None = None,
        include_file: bool = False,
        publish: bool = True,
        key: str,
    ) -> tuple[dict[str, Any], OracleSnapshot]: ...


class CallbackTrialOracle:
    """Exact HTTPS callback mode for private, caller-owned test networks."""

    transport = "caller-owned-write-only-https"

    def __init__(self, receiver: ObserverReceiver) -> None:
        self.receiver = receiver

    async def reset(self, driver: McpDriver, *, key: str) -> object:
        latest = self.receiver.latest()
        after_sequence = latest.sequence if latest else 0
        reset = await driver.reset_observer(key)
        if reset.get("status") != "completed":
            raise RuntimeError(
                f"observer reset did not complete: {reset.get('status')}"
            )
        baseline = await asyncio.to_thread(
            self.receiver.wait_for_sequence,
            after_sequence,
            timeout_s=15,
        )
        if baseline is None:
            raise RuntimeError("observer reset produced no baseline report")
        if baseline.text or baseline.dangerous_commits:
            raise RuntimeError("observer reset baseline was not empty")
        trial_id, _ = self.receiver.begin_trial()
        return trial_id

    async def seal(
        self,
        driver: McpDriver,
        trial: object,
        *,
        intended: str,
        ocr_text: str | None = None,
        expected_file: bytes | None = None,
        include_file: bool = False,
        publish: bool = True,
        key: str,
    ) -> tuple[dict[str, Any], OracleSnapshot]:
        if publish:
            await driver.publish_observer(include_file=include_file, key=key)
        score = self.receiver.seal_trial(
            str(trial),
            intended=intended,
            ocr_text=ocr_text,
            expected_file=expected_file,
            timeout_s=15,
        )
        snapshot = self.receiver.latest()
        if snapshot is None:
            raise RuntimeError("callback oracle did not retain its sealed snapshot")
        return score.model_dump(), snapshot


class VisualTrialOracle:
    """Exact oracle decoded only from screenshots returned by the MCP server."""

    transport = "mcp-screenshot-visual-matrix"

    async def reset(self, driver: McpDriver, *, key: str) -> object:
        reset = await driver.reset_observer(key)
        if reset.get("status") != "completed":
            raise RuntimeError(
                f"observer reset did not complete: {reset.get('status')}"
            )
        return uuid.uuid4().hex

    async def _collect(self, driver: McpDriver, *, key: str) -> OracleSnapshot:
        pages: dict[int, Any] = {}
        page_count: int | None = None
        try:
            for action_index in range(1024):
                await driver.screenshot()
                if driver.last_image is None:
                    raise VisualOracleError("MCP screenshot contained no image")
                page = decode_page(driver.last_image)
                print(
                    f"benchmark oracle: page {page.page_index + 1}/"
                    f"{page.page_count}",
                    flush=True,
                )
                if page_count is not None and page.page_count != page_count:
                    raise VisualOracleError(
                        "visual oracle page count changed during collection"
                    )
                page_count = page.page_count
                previous = pages.get(page.page_index)
                if previous is not None and previous != page:
                    raise VisualOracleError(
                        "visual oracle page changed during collection"
                    )
                pages[page.page_index] = page
                if len(pages) >= page_count:
                    break
                missing = [
                    index
                    for index in range(page_count)
                    if index not in pages
                ]
                target = min(
                    missing,
                    key=lambda index: (
                        abs(index - page.page_index),
                        index,
                    ),
                )
                direction = "F8" if target > page.page_index else "F7"
                advanced = await driver.burst(
                    [
                        {
                            "type": "key",
                            "keys": ["CTRL", "SHIFT", direction],
                        }
                    ],
                    key=f"{key}-page-action-{action_index + 1}",
                )
                if advanced.get("status") != "completed":
                    raise VisualOracleError(
                        "could not navigate visual oracle: "
                        f"{advanced.get('status')}"
                    )
            if page_count is None or len(pages) != page_count:
                raise VisualOracleError("visual oracle exceeded page collection limit")
            payload = assemble_pages(
                [pages[index] for index in range(page_count)]
            )
            return OracleSnapshot.model_validate_json(payload)
        finally:
            # F12 closes the matrix and returns focus to the helper editor.
            await driver.burst(
                [{"type": "key", "keys": ["CTRL", "SHIFT", "F12"]}],
                key=f"{key}-close",
            )

    async def seal(
        self,
        driver: McpDriver,
        trial: object,
        *,
        intended: str,
        ocr_text: str | None = None,
        expected_file: bytes | None = None,
        include_file: bool = False,
        publish: bool = True,
        key: str,
    ) -> tuple[dict[str, Any], OracleSnapshot]:
        del trial
        snapshot: OracleSnapshot | None = None
        last_error: VisualOracleError | None = None
        for attempt in range(3):
            if publish or attempt > 0:
                published = await driver.publish_observer(
                    include_file=include_file,
                    key=f"{key}-publish-{attempt + 1}",
                )
                if published.get("status") != "completed":
                    raise VisualOracleError(
                        "observer publish did not complete: "
                        f"{published.get('status')}"
                    )
            try:
                snapshot = await self._collect(
                    driver,
                    key=f"{key}-attempt-{attempt + 1}",
                )
                break
            except VisualOracleError as exc:
                last_error = exc
                if attempt == 2:
                    raise VisualOracleError(
                        "visual oracle remained corrupt after 3 independent "
                        f"captures: {exc}"
                    ) from exc
                print(
                    f"benchmark oracle: corrupt capture, retrying "
                    f"({attempt + 1}/3): {exc}",
                    flush=True,
                )
        if snapshot is None:
            raise last_error or VisualOracleError("visual oracle returned no snapshot")
        score = score_snapshot(
            intended=intended,
            snapshot=snapshot,
            ocr_text=ocr_text,
            expected_file=expected_file,
        )
        return score.model_dump(), snapshot


async def _reset_and_begin_trial(
    driver: McpDriver,
    oracle: TrialOracle,
    *,
    key: str,
) -> object:
    return await oracle.reset(driver, key=key)


async def _timed_trial(
    name: str,
    trial: Awaitable[dict[str, Any]],
) -> dict[str, Any]:
    print(f"benchmark trial: {name} started", flush=True)
    started = time.monotonic()
    try:
        result = await trial
    except Exception as exc:  # noqa: BLE001 - preserve completed benchmark evidence
        elapsed_ms = round((time.monotonic() - started) * 1000)
        print(
            f"benchmark trial: {name} failed in {elapsed_ms} ms: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return {
            "status": "harness_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "elapsed_ms": elapsed_ms,
        }
    elapsed_ms = round((time.monotonic() - started) * 1000)
    result["elapsed_ms"] = elapsed_ms
    print(
        f"benchmark trial: {name} completed in {elapsed_ms} ms",
        flush=True,
    )
    return result


async def _record_trial(
    report: dict[str, Any],
    name: str,
    trial: Awaitable[dict[str, Any]],
) -> bool:
    """Retain one result and fail closed before any later benchmark actions."""
    result = await _timed_trial(name, trial)
    report["trials"][name] = result
    if result.get("status") != "harness_error":
        return True
    report["aborted_after"] = name
    return False


async def _run_text_trial(
    driver: McpDriver,
    oracle: TrialOracle,
    *,
    name: str,
    intended: str,
    code: bool,
) -> dict[str, Any]:
    trial_id = await _reset_and_begin_trial(
        driver,
        oracle,
        key=f"{name}-reset",
    )
    outcomes = await driver.type_chunks(
        intended,
        key_prefix=f"{name}-type",
        code=code,
    )
    ocr_text, ocr_region = await driver.ocr_editor()
    score, snapshot = await oracle.seal(
        driver,
        trial_id,
        intended=intended,
        ocr_text=ocr_text,
        key=f"{name}-oracle",
    )
    return {
        "score": score,
        "burst_statuses": [outcome.get("status") for outcome in outcomes],
        "ocr_text": ocr_text,
        "ocr_region": ocr_region,
        "foreground_executable": snapshot.foreground_executable,
        "environment": _safe_observer_environment(snapshot),
    }


async def _run_environment_identity_trial(
    driver: McpDriver,
    oracle: TrialOracle,
) -> dict[str, Any]:
    trial_id = await _reset_and_begin_trial(
        driver,
        oracle,
        key="environment-identity-reset",
    )
    score, snapshot = await oracle.seal(
        driver,
        trial_id,
        intended="",
        key="environment-identity-oracle",
    )
    return {
        "score": score,
        "environment": _safe_observer_environment(snapshot),
    }


async def _run_idempotency_trial(
    driver: McpDriver,
    oracle: TrialOracle,
) -> dict[str, Any]:
    trial_id = await _reset_and_begin_trial(
        driver,
        oracle,
        key="idempotency-reset",
    )
    actions = [
        {
            "type": "type_text",
            "text": IDEMPOTENCY_SENTINEL,
            "method": "print",
            "context": "editor",
        }
    ]
    first = await driver.burst(actions, key="idempotency-type-once")
    replay = await driver.burst(actions, key="idempotency-type-once")
    score, _snapshot = await oracle.seal(
        driver,
        trial_id,
        intended=IDEMPOTENCY_SENTINEL,
        key="idempotency-oracle",
    )
    return {
        "first_status": first.get("status"),
        "replay_status": replay.get("status"),
        "idempotent_replay": replay.get("idempotent_replay") is True,
        "score": score,
    }


async def _run_click_trial(
    driver: McpDriver,
    oracle: TrialOracle,
) -> dict[str, Any]:
    trial_id = await _reset_and_begin_trial(
        driver,
        oracle,
        key="click-reset",
    )
    matches = await driver.find_text("Copy snapshot")
    if not matches:
        return {"status": "failed_grounding", "matches": []}
    target = matches[0]
    click = await driver.burst(
        [
            {
                "type": "click",
                "x": int(target["center"][0]),
                "y": int(target["center"][1]),
            }
        ],
        key="click-copy-snapshot",
    )
    score, _snapshot = await oracle.seal(
        driver,
        trial_id,
        intended="",
        publish=False,
        key="click-oracle",
    )
    return {
        "status": click.get("status"),
        "target": target,
        "score": score,
    }


async def _run_dangerous_click_trial(
    driver: McpDriver,
    oracle: TrialOracle,
) -> dict[str, Any]:
    trial_id = await _reset_and_begin_trial(
        driver,
        oracle,
        key="danger-reset",
    )
    matches = await driver.find_text("Send")
    if not matches:
        return {"status": "failed_grounding", "matches": []}
    target = matches[-1]
    click = await driver.burst(
        [
            {
                "type": "click",
                "x": int(target["center"][0]),
                "y": int(target["center"][1]),
            }
        ],
        key="danger-send-click",
    )
    guard_status = str(click.get("status"))
    risk = click.get("risk") or (click.get("approval_request") or {}).get("risk")
    if guard_status == "needs_approval":
        await driver.reject_approval(click)
    await driver.screenshot()
    score, _snapshot = await oracle.seal(
        driver,
        trial_id,
        intended="",
        key="danger-oracle",
    )
    return {
        "status": guard_status,
        "risk": risk,
        "target": target,
        "score": score,
    }


async def _run_editor_trial(
    driver: McpDriver,
    oracle: TrialOracle,
    *,
    name: str,
    definition: dict[str, Any],
) -> dict[str, Any]:
    trial_id = await _reset_and_begin_trial(
        driver,
        oracle,
        key=f"editor-{name}-reset",
    )
    launched = await driver.playbook(
        "windows.run",
        {"command": definition["command"]},
        key=f"editor-{name}-launch",
        max_runtime_ms=45000,
    )
    launch_prompted = launched.get("status") == "needs_approval"
    if launch_prompted:
        launched = await driver.approve_fixture_write(launched)
    if launched.get("status") != "completed":
        return {
            "status": "launch_failed",
            "launch_status": launched.get("status"),
            "error": launched.get("error"),
        }

    async def read_screen() -> dict[str, Any]:
        return await driver.call(
            "pikvm_ocr_region",
            {
                "session_id": driver.session_id,
                "x": 0,
                "y": 0,
                "w": driver.width,
                "h": driver.height,
            },
        )

    screen = await read_screen()
    screen_history = [str(screen.get("text", ""))]
    activation_target = None
    activation_cycles = 0
    for attempt in range(4):
        screen_text = str(screen.get("text", ""))
        lowered = screen_text.lower()
        if "cannot find" in lowered or "not recognized" in lowered:
            return {"status": "unavailable", "ocr_text": screen_text}
        activation_target = _editor_activation_target(
            list(screen.get("lines") or []),
            expected_title=str(definition["expected_title"]),
            screen_height=driver.height,
            required_markers=list(definition.get("activation_markers") or []),
        )
        if activation_target is not None:
            break
        if attempt == 3:
            break
        switch = await driver.burst(
            [{"type": "key", "keys": ["ALT", "TAB"]}],
            key=f"editor-{name}-activation-cycle-{attempt}",
        )
        if switch.get("status") != "completed":
            return {
                "status": "activation_cycle_failed",
                "activation_status": switch.get("status"),
                "activation_cycles": activation_cycles,
            }
        activation_cycles += 1
        screen = await read_screen()
        screen_history.append(str(screen.get("text", "")))
    if activation_target is None:
        return {
            "status": "activation_grounding_failed",
            "launch_ocr": screen_history,
            "activation_cycles": activation_cycles,
        }
    activation = await driver.burst(
        [
            {
                "type": "click",
                "x": int(activation_target["x"]),
                "y": int(activation_target["y"]),
                "target_text": str(activation_target["text"]),
            }
        ],
        key=f"editor-{name}-activate-window",
    )
    activation_prompted = activation.get("status") == "needs_approval"
    if activation_prompted:
        activation = await driver.approve_fixture_write(activation)
    if activation.get("status") != "completed":
        return {
            "status": "activation_failed",
            "activation_status": activation.get("status"),
            "activation_prompted": activation_prompted,
            "activation_target": activation_target,
            "activation_cycles": activation_cycles,
        }

    focus = await driver.burst(
        [
            {
                "type": "click",
                "x": round(driver.width * 0.55),
                "y": round(driver.height * 0.43),
                "target_text": "editor content",
            }
        ],
        key=f"editor-{name}-focus-content",
    )
    focus_prompted = focus.get("status") == "needs_approval"
    if focus_prompted:
        focus = await driver.approve_fixture_write(focus)
    if focus.get("status") != "completed":
        return {
            "status": "focus_failed",
            "focus_status": focus.get("status"),
            "focus_prompted": focus_prompted,
            "activation_target": activation_target,
            "activation_prompted": activation_prompted,
            "activation_cycles": activation_cycles,
        }
    await driver.burst(
        [{"type": "key", "keys": ["CTRL", "A"]}],
        key=f"editor-{name}-select-all",
    )
    typing = await driver.type_chunks(
        definition["content"],
        key_prefix=f"editor-{name}-type",
        code=name != "notepad",
    )
    typing_statuses = [item.get("status") for item in typing]
    if any(status != "completed" for status in typing_statuses):
        score, snapshot = await oracle.seal(
            driver,
            trial_id,
            intended=definition["content"],
            key=f"editor-{name}-typing-failed-oracle",
        )
        return {
            "status": "typing_failed",
            "launch_prompted": launch_prompted,
            "activation_target": activation_target,
            "activation_prompted": activation_prompted,
            "activation_cycles": activation_cycles,
            "focus_prompted": focus_prompted,
            "launch_ocr": screen_text,
            "typing_statuses": typing_statuses,
            "save_attempted": False,
            "foreground_executable": snapshot.foreground_executable,
            "foreground_title": snapshot.foreground_title,
            "environment": _safe_observer_environment(snapshot),
            "score": score,
        }
    save = await driver.burst(
        [{"type": "key", "keys": ["CTRL", "S"]}],
        key=f"editor-{name}-save",
    )
    prompted = save.get("status") == "needs_approval"
    if prompted:
        save = await driver.approve_fixture_write(save)
    score, snapshot = await oracle.seal(
        driver,
        trial_id,
        intended="",
        expected_file=definition["content"].encode("utf-8"),
        include_file=True,
        key=f"editor-{name}-oracle",
    )
    expected_executable = definition["expected_executable"].lower()
    actual_executable = snapshot.foreground_executable.lower()
    identity_matches = actual_executable == expected_executable
    return {
        "status": "completed" if identity_matches else "wrong_foreground_app",
        "launch_prompted": launch_prompted,
        "activation_target": activation_target,
        "activation_prompted": activation_prompted,
        "activation_cycles": activation_cycles,
        "focus_prompted": focus_prompted,
        "launch_ocr": screen_text,
        "typing_statuses": typing_statuses,
        "save_prompted": prompted,
        "save_status": save.get("status"),
        "expected_foreground_executable": definition["expected_executable"],
        "foreground_executable": snapshot.foreground_executable,
        "foreground_title": snapshot.foreground_title,
        "environment": _safe_observer_environment(snapshot),
        "score": score,
    }


async def _run_trials(
    *,
    endpoint: str,
    keymap: str,
    password: str | None,
    username: str | None,
    editors: list[str],
    oracle: TrialOracle,
    identity_only: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pikvm-mcp-lab-") as root:
        ports = allocate_lab_ports()
        with RunningLab(
            endpoint=endpoint,
            root=Path(root),
            ports=ports,
            executable=os.path.abspath(sys.executable),
            keymap=keymap,
            password=password,
            username=username,
            keyboard_profile="windows",
            quiet=True,
        ) as lab:
            print(
                f"benchmark phase: isolated lab ready on "
                f"{ports.adapter}/{ports.daemon}",
                flush=True,
            )
            env = dict(lab.env)
            env["PIKVM_AGENT_DAEMON"] = lab.daemon_url
            params = StdioServerParameters(
                command=os.path.abspath(sys.executable),
                args=["-m", "pikvm_agent.cli", "mcp"],
                env=env,
                cwd=str(Path(__file__).resolve().parents[2]),
            )
            with open(os.devnull, "w") as mcp_errors:
                transport = stdio_client(params, errlog=mcp_errors)
                async with transport as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        print("benchmark phase: MCP stdio ready", flush=True)
                        driver = McpDriver(session)
                        await driver.open()
                        report: dict[str, Any] = {
                            "protocol": "pikvm-harness-report.v1",
                            "control_plane": "mcp-stdio",
                            "target_adapter": "vnc-pikvm-emulator",
                            "observer_transport": oracle.transport,
                            "observer_environment_identity_required": True,
                            "production_daemon_port_rejected": 47615,
                            "screen": {
                                "width": driver.width,
                                "height": driver.height,
                            },
                            "trials": {},
                        }
                        if identity_only:
                            await _record_trial(
                                report,
                                "environment_identity",
                                _run_environment_identity_trial(driver, oracle),
                            )
                            await driver.call(
                                "pikvm_abort",
                                {
                                    "session_id": driver.session_id,
                                    "reason": "observer identity probe complete",
                                },
                            )
                            environment = (
                                report["trials"]
                                .get("environment_identity", {})
                                .get("environment", {})
                            )
                            failures = _environment_identity_failures(
                                "environment_identity", environment
                            )
                            report["status"] = (
                                "passed" if not failures else "failed"
                            )
                            report["failures"] = failures
                            return report
                        should_continue = await _record_trial(
                            report,
                            "long_prose",
                            _run_text_trial(
                                driver,
                                oracle,
                                name="prose",
                                intended=PROSE,
                                code=False,
                            ),
                        )
                        if should_continue:
                            should_continue = await _record_trial(
                                report,
                                "code",
                                _run_text_trial(
                                    driver,
                                    oracle,
                                    name="code",
                                    intended=CODE,
                                    code=True,
                                ),
                            )
                        if should_continue:
                            should_continue = await _record_trial(
                                report,
                                "duplicate_retry",
                                _run_idempotency_trial(driver, oracle),
                            )
                        if should_continue:
                            should_continue = await _record_trial(
                                report,
                                "ocr_grounded_click",
                                _run_click_trial(driver, oracle),
                            )
                        if should_continue:
                            should_continue = await _record_trial(
                                report,
                                "dangerous_send_guard",
                                _run_dangerous_click_trial(driver, oracle),
                            )
                        for editor in editors:
                            if not should_continue:
                                break
                            definition = EDITOR_CASES.get(editor)
                            if definition is None:
                                report["trials"][f"editor_{editor}"] = {
                                    "status": "unknown_editor"
                                }
                                continue
                            should_continue = await _record_trial(
                                report,
                                f"editor_{editor}",
                                _run_editor_trial(
                                    driver,
                                    oracle,
                                    name=editor,
                                    definition=definition,
                                ),
                            )
                        await driver.call(
                            "pikvm_abort",
                            {
                                "session_id": driver.session_id,
                                "reason": (
                                    "benchmark complete"
                                    if should_continue
                                    else "benchmark halted after harness error"
                                ),
                            },
                        )
                        failures = evaluate_report(report)
                        report["status"] = "passed" if not failures else "failed"
                        report["failures"] = failures
                        return report


async def run_benchmark(
    *,
    endpoint: str,
    artifact: Path | None,
    artifact_url: str | None,
    observer_public_base_url: str | None,
    receiver_bind_host: str,
    receiver_port: int,
    keymap: str,
    password: str | None,
    username: str | None,
    editors: list[str],
    observer_mode: str = "visual",
    skip_provision: bool = False,
    identity_only: bool = False,
) -> dict[str, Any]:
    if observer_mode == "visual":
        if not skip_provision:
            if artifact_url is None:
                raise ValueError(
                    "visual mode provisioning requires --artifact-url "
                    "(or use --skip-provision)"
                )
            deploy(
                endpoint=endpoint,
                artifact_url=artifact_url,
                password=password,
                username=username,
            )
        print("benchmark phase: visual observer ready", flush=True)
        return await _run_trials(
            endpoint=endpoint,
            keymap=keymap,
            password=password,
            username=username,
            editors=editors,
            oracle=VisualTrialOracle(),
            identity_only=identity_only,
        )

    if observer_mode != "https":
        raise ValueError("observer_mode must be 'visual' or 'https'")
    if skip_provision:
        raise ValueError(
            "--skip-provision is supported only by screenshot visual mode"
        )
    if artifact is None or observer_public_base_url is None:
        raise ValueError(
            "https mode requires --artifact and --observer-public-base-url"
        )
    token = secrets.token_hex(16)
    with RunningObserverReceiver(
        artifact=artifact,
        token=token,
        host=receiver_bind_host,
        port=receiver_port,
    ) as observer:
        print("benchmark phase: observer receiver ready", flush=True)
        if not skip_provision:
            deploy(
                endpoint=endpoint,
                public_base_url=observer_public_base_url,
                token=token,
                password=password,
                username=username,
            )
        initial = observer.receiver.wait_for_sequence(0, timeout_s=30)
        if initial is None:
            raise RuntimeError("observer did not report after provisioning")
        return await _run_trials(
            endpoint=endpoint,
            keymap=keymap,
            password=password,
            username=username,
            editors=editors,
            oracle=CallbackTrialOracle(observer.receiver),
            identity_only=identity_only,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run blind end-to-end MCP accuracy trials on a disposable VNC target."
    )
    parser.add_argument("--vnc", required=True)
    parser.add_argument(
        "--observer-mode",
        choices=("visual", "https"),
        default="visual",
        help="Exact-oracle transport; visual uses MCP screenshots only.",
    )
    parser.add_argument("--artifact", type=Path)
    parser.add_argument(
        "--artifact-url",
        help="Caller-owned HTTPS observer binary URL for visual provisioning.",
    )
    parser.add_argument(
        "--observer-public-base-url",
        help=(
            "Caller-owned HTTPS URL routed to the local write-only observer "
            "receiver in https mode; quick tunnels are never created."
        ),
    )
    parser.add_argument(
        "--skip-provision",
        action="store_true",
        help="Use an observer already running on the disposable target.",
    )
    parser.add_argument(
        "--identity-only",
        action="store_true",
        help=(
            "Provision and prove guest/foreground/focus identity without "
            "running the slower typing, OCR, click, and editor matrix."
        ),
    )
    parser.add_argument("--receiver-bind-host", default="127.0.0.1")
    parser.add_argument("--receiver-port", default=47642, type=int)
    parser.add_argument("--keymap", default="en-us")
    parser.add_argument(
        "--password-env",
        default="PIKVM_LAB_VNC_PASSWORD",
        help="Environment variable containing the VNC password.",
    )
    parser.add_argument(
        "--username-env",
        default="PIKVM_LAB_VNC_USERNAME",
        help="Environment variable containing the VNC username.",
    )
    parser.add_argument(
        "--editors",
        default="notepad,vscode,notepad++",
        help="Comma-separated editor matrix.",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(
            run_benchmark(
                endpoint=args.vnc,
                artifact=args.artifact,
                artifact_url=args.artifact_url,
                observer_public_base_url=args.observer_public_base_url,
                receiver_bind_host=args.receiver_bind_host,
                receiver_port=args.receiver_port,
                keymap=args.keymap,
                password=os.environ.get(args.password_env),
                username=os.environ.get(args.username_env),
                editors=[
                    item.strip()
                    for item in args.editors.split(",")
                    if item.strip()
                ],
                observer_mode=args.observer_mode,
                skip_provision=args.skip_provision,
                identity_only=args.identity_only,
            )
        )
    except BaseException as exc:
        failure = {
            "protocol": "pikvm-harness-report.v1",
            "status": "harness_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        rendered = json.dumps(failure, indent=2)
        if args.report:
            args.report.write_text(rendered + "\n")
        print(rendered, flush=True)
        raise
    rendered = json.dumps(report, indent=2)
    if args.report:
        args.report.write_text(rendered + "\n")
    print(rendered)
    if report.get("status") != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
